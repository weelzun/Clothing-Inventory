import { useEffect, useState } from 'react';
import {
  ActivityIndicator,
  KeyboardAvoidingView,
  Platform,
  Pressable,
  ScrollView,
  StyleSheet,
  Text,
  TextInput,
  View,
} from 'react-native';
import { SafeAreaProvider, SafeAreaView } from 'react-native-safe-area-context';
import { CameraView, useCameraPermissions } from 'expo-camera';
import * as SecureStore from 'expo-secure-store';
import { StatusBar } from 'expo-status-bar';

// ----------------------------------------------------------------------
// Modes: what happens after you scan
// ----------------------------------------------------------------------
const MODES = [
  { id: 'lookup', label: 'Look up' },
  { id: 'receive', label: 'Receive', action: 'receive', verb: 'Received' },
  { id: 'sell', label: 'Sell', action: 'sale', verb: 'Sold' },
  { id: 'return', label: 'Return', action: 'return', verb: 'Returned' },
  { id: 'count', label: 'Count' },
];

const DEFAULT_URL = 'http://192.168.0.5:8000'; // your PC's address; change in Settings

// Colombian peso format: 100000 -> $100.000 (no decimals)
function formatCOP(amount) {
  const whole = Math.round(Number(amount) || 0);
  return '$' + String(whole).replace(/\B(?=(\d{3})+(?!\d))/g, '.');
}

// ----------------------------------------------------------------------
// Talking to the server
// ----------------------------------------------------------------------
async function api(cfg, path, { method = 'GET', body } = {}) {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), 8000);
  try {
    const res = await fetch(cfg.url.replace(/\/+$/, '') + path, {
      method,
      headers: { 'X-API-Key': cfg.key, 'Content-Type': 'application/json' },
      body: body ? JSON.stringify(body) : undefined,
      signal: controller.signal,
    });
    const data = await res.json().catch(() => null);
    if (!res.ok) {
      const detail = data && typeof data.detail === 'string' ? data.detail : null;
      if (res.status === 401) throw new Error('Wrong API key. Check Settings.');
      throw new Error(detail || `Server error (${res.status})`);
    }
    return data;
  } catch (e) {
    if (e.name === 'AbortError' || e instanceof TypeError) {
      throw new Error("Can't reach the server. Check Wi-Fi and the server address in Settings.");
    }
    throw e;
  } finally {
    clearTimeout(timer);
  }
}

// ----------------------------------------------------------------------
// Settings screen
// ----------------------------------------------------------------------
function Settings({ initial, onSave, onCancel }) {
  const [url, setUrl] = useState(initial?.url || DEFAULT_URL);
  const [key, setKey] = useState(initial?.key || '');
  const [status, setStatus] = useState(null);
  const [busy, setBusy] = useState(false);

  const test = async () => {
    setBusy(true);
    setStatus(null);
    try {
      // 404 means the key worked and the item just doesn't exist, which is what we want.
      await api({ url: url.trim(), key: key.trim() }, '/items/__ping__');
    } catch (e) {
      if (e.message === 'No item with that barcode') {
        setStatus({ ok: true, text: 'Connected. Server and key are working.' });
      } else {
        setStatus({ ok: false, text: e.message });
      }
      setBusy(false);
      return;
    }
    setStatus({ ok: true, text: 'Connected.' });
    setBusy(false);
  };

  return (
    <KeyboardAvoidingView
      style={{ flex: 1 }}
      behavior={Platform.OS === 'ios' ? 'padding' : undefined}
    >
      <ScrollView contentContainerStyle={s.settingsBody} keyboardShouldPersistTaps="handled">
        <Text style={s.h1}>Settings</Text>

        <Text style={s.label}>Server address</Text>
        <TextInput
          style={s.input}
          value={url}
          onChangeText={setUrl}
          autoCapitalize="none"
          autoCorrect={false}
          keyboardType="url"
          placeholder="http://192.168.0.5:8000"
          placeholderTextColor="#667"
        />

        <Text style={s.label}>API key</Text>
        <TextInput
          style={s.input}
          value={key}
          onChangeText={setKey}
          autoCapitalize="none"
          autoCorrect={false}
          secureTextEntry
          placeholder="The INVENTORY_API_KEY you set on the PC"
          placeholderTextColor="#667"
        />

        {status && (
          <Text style={[s.banner, status.ok ? s.bannerOk : s.bannerErr]}>{status.text}</Text>
        )}

        <Pressable style={[s.btn, s.btnGhost]} onPress={test} disabled={busy}>
          {busy ? <ActivityIndicator color="#fff" /> : <Text style={s.btnText}>Test connection</Text>}
        </Pressable>
        <Pressable
          style={[s.btn, s.btnPrimary, (!url.trim() || !key.trim()) && s.btnDisabled]}
          onPress={() => onSave({ url: url.trim(), key: key.trim() })}
          disabled={!url.trim() || !key.trim()}
        >
          <Text style={s.btnText}>Save</Text>
        </Pressable>
        {onCancel && (
          <Pressable style={[s.btn, s.btnGhost]} onPress={onCancel}>
            <Text style={s.btnText}>Cancel</Text>
          </Pressable>
        )}
      </ScrollView>
    </KeyboardAvoidingView>
  );
}

// ----------------------------------------------------------------------
// Scanner screen
// ----------------------------------------------------------------------
function Scanner({ cfg, onOpenSettings }) {
  const [permission, requestPermission] = useCameraPermissions();
  const [mode, setMode] = useState('lookup');
  const [locked, setLocked] = useState(false); // true while a result is on screen
  const [busy, setBusy] = useState(false);
  const [item, setItem] = useState(null);
  const [qty, setQty] = useState(1);
  const [result, setResult] = useState(null);
  const [error, setError] = useState(null);

  const current = MODES.find((m) => m.id === mode);

  const reset = () => {
    setLocked(false);
    setBusy(false);
    setItem(null);
    setResult(null);
    setError(null);
    setQty(1);
  };

  const changeMode = (id) => {
    setMode(id);
    reset();
  };

  const handleScan = async ({ data }) => {
    if (locked) return;
    setLocked(true);
    setBusy(true);
    setError(null);
    setResult(null);
    try {
      const found = await api(cfg, `/items/${encodeURIComponent(data)}`);
      setItem(found);
      setQty(mode === 'count' ? found.quantity : 1);
    } catch (e) {
      setError(e.message === 'No item with that barcode' ? `Barcode not found: ${data}` : e.message);
    } finally {
      setBusy(false);
    }
  };

  const confirm = async () => {
    setBusy(true);
    setError(null);
    try {
      let updated;
      if (mode === 'count') {
        updated = await api(cfg, '/stock/count', {
          method: 'POST',
          body: { barcode: item.barcode, counted_qty: qty },
        });
        setResult(`Counted ${qty}`);
      } else {
        updated = await api(cfg, '/stock/adjust', {
          method: 'POST',
          body: { barcode: item.barcode, action: current.action, quantity: qty },
        });
        setResult(`${current.verb} ${qty}`);
      }
      setItem(updated);
    } catch (e) {
      setError(e.message);
    } finally {
      setBusy(false);
    }
  };

  const minQty = mode === 'count' ? 0 : 1;
  const setQtyText = (t) => {
    const n = parseInt(t.replace(/[^0-9]/g, ''), 10);
    setQty(Number.isNaN(n) ? minQty : n);
  };

  // ---- camera permission states ----
  if (!permission) return <View style={s.center}><ActivityIndicator color="#fff" /></View>;
  if (!permission.granted) {
    return (
      <View style={s.center}>
        <Text style={s.h1}>Camera access needed</Text>
        <Text style={s.muted}>The app uses the camera to scan barcode labels.</Text>
        <Pressable style={[s.btn, s.btnPrimary]} onPress={requestPermission}>
          <Text style={s.btnText}>Allow camera</Text>
        </Pressable>
      </View>
    );
  }

  const lowStock = item && item.quantity <= item.reorder_level;

  return (
    <View style={{ flex: 1 }}>
      {/* Top bar */}
      <View style={s.topBar}>
        <Text style={s.title}>Inventory</Text>
        <Pressable onPress={onOpenSettings} hitSlop={12}>
          <Text style={s.gear}>Settings</Text>
        </Pressable>
      </View>

      {/* Mode chips */}
      <ScrollView horizontal showsHorizontalScrollIndicator={false} style={s.chipRow}
        contentContainerStyle={{ paddingHorizontal: 12, gap: 8 }}>
        {MODES.map((m) => (
          <Pressable key={m.id} onPress={() => changeMode(m.id)}
            style={[s.chip, mode === m.id && s.chipActive]}>
            <Text style={[s.chipText, mode === m.id && s.chipTextActive]}>{m.label}</Text>
          </Pressable>
        ))}
      </ScrollView>

      {/* Camera */}
      <View style={s.cameraBox}>
        <CameraView
          style={StyleSheet.absoluteFill}
          facing="back"
          barcodeScannerSettings={{ barcodeTypes: ['code128', 'ean13', 'upc_a', 'qr'] }}
          onBarcodeScanned={locked ? undefined : handleScan}
        />
        {!locked && (
          <View style={s.frameWrap} pointerEvents="none">
            <View style={s.frame} />
            <Text style={s.hint}>Point at a barcode label</Text>
          </View>
        )}
      </View>

      {/* Result panel */}
      {locked && (
        <View style={s.panel}>
          {busy && !item && <ActivityIndicator color="#fff" />}

          {item && (
            <>
              <Text style={s.itemName}>{item.product_name}</Text>
              <Text style={s.itemSub}>
                Size {item.size} · {formatCOP(item.retail_price)}
              </Text>
              <Text style={s.sku}>{item.sku}</Text>
              <Text style={[s.stock, lowStock && s.stockLow]}>
                In stock: {item.quantity}{lowStock ? '  (low)' : ''}
              </Text>
            </>
          )}

          {result && <Text style={[s.banner, s.bannerOk]}>{result}. Now {item.quantity} in stock.</Text>}
          {error && <Text style={[s.banner, s.bannerErr]}>{error}</Text>}

          {/* Quantity + confirm (only for changing modes, before a result) */}
          {item && !result && mode !== 'lookup' && (
            <>
              <View style={s.stepper}>
                <Pressable style={s.stepBtn} onPress={() => setQty(Math.max(minQty, qty - 1))}>
                  <Text style={s.stepText}>−</Text>
                </Pressable>
                <TextInput
                  style={s.qtyInput}
                  value={String(qty)}
                  onChangeText={setQtyText}
                  keyboardType="number-pad"
                  selectTextOnFocus
                />
                <Pressable style={s.stepBtn} onPress={() => setQty(qty + 1)}>
                  <Text style={s.stepText}>+</Text>
                </Pressable>
              </View>
              <Pressable style={[s.btn, s.btnPrimary]} onPress={confirm} disabled={busy}>
                {busy ? <ActivityIndicator color="#fff" /> : (
                  <Text style={s.btnText}>
                    {mode === 'count' ? `Set count to ${qty}` : `${current.label} ${qty}`}
                  </Text>
                )}
              </Pressable>
              <Pressable style={[s.btn, s.btnGhost]} onPress={reset}>
                <Text style={s.btnText}>Cancel</Text>
              </Pressable>
            </>
          )}

          {/* Scan next: after a result, in look-up mode, or after an error with no item */}
          {(result || mode === 'lookup' || (!item && !busy)) && (
            <Pressable style={[s.btn, s.btnPrimary]} onPress={reset}>
              <Text style={s.btnText}>Scan next</Text>
            </Pressable>
          )}
        </View>
      )}
    </View>
  );
}

// ----------------------------------------------------------------------
// App root
// ----------------------------------------------------------------------
function Root() {
  const [loaded, setLoaded] = useState(false);
  const [cfg, setCfg] = useState(null);
  const [screen, setScreen] = useState('scan');

  useEffect(() => {
    (async () => {
      try {
        const url = await SecureStore.getItemAsync('serverUrl');
        const key = await SecureStore.getItemAsync('apiKey');
        if (url && key) setCfg({ url, key });
        else setScreen('settings');
      } catch {
        setScreen('settings');
      }
      setLoaded(true);
    })();
  }, []);

  const save = async (next) => {
    try {
      await SecureStore.setItemAsync('serverUrl', next.url);
      await SecureStore.setItemAsync('apiKey', next.key);
    } catch {
      // If saving fails we still use the values for this session.
    }
    setCfg(next);
    setScreen('scan');
  };

  if (!loaded) return <View style={s.center}><ActivityIndicator color="#fff" /></View>;

  return screen === 'settings' || !cfg ? (
    <Settings initial={cfg} onSave={save} onCancel={cfg ? () => setScreen('scan') : null} />
  ) : (
    <Scanner cfg={cfg} onOpenSettings={() => setScreen('settings')} />
  );
}

export default function App() {
  return (
    <SafeAreaProvider>
      <SafeAreaView style={s.root}>
        <StatusBar style="light" />
        <Root />
      </SafeAreaView>
    </SafeAreaProvider>
  );
}

// ----------------------------------------------------------------------
// Styles
// ----------------------------------------------------------------------
const s = StyleSheet.create({
  root: { flex: 1, backgroundColor: '#0f1419' },
  center: { flex: 1, alignItems: 'center', justifyContent: 'center', padding: 24, gap: 12 },
  topBar: { flexDirection: 'row', justifyContent: 'space-between', alignItems: 'center',
    paddingHorizontal: 16, paddingVertical: 10 },
  title: { color: '#fff', fontSize: 20, fontWeight: '700' },
  gear: { color: '#7cc4ff', fontSize: 16 },
  chipRow: { flexGrow: 0, paddingBottom: 10 },
  chip: { paddingHorizontal: 14, paddingVertical: 8, borderRadius: 20, backgroundColor: '#1e2730' },
  chipActive: { backgroundColor: '#2f81f7' },
  chipText: { color: '#9fb0c0', fontWeight: '600' },
  chipTextActive: { color: '#fff' },
  cameraBox: { flex: 1, backgroundColor: '#000', overflow: 'hidden' },
  frameWrap: { ...StyleSheet.absoluteFillObject, alignItems: 'center', justifyContent: 'center' },
  frame: { width: '78%', height: 130, borderWidth: 3, borderColor: '#7cc4ff', borderRadius: 14 },
  hint: { color: '#fff', marginTop: 14, fontSize: 15 },
  panel: { backgroundColor: '#161d25', padding: 16, gap: 8, borderTopLeftRadius: 18,
    borderTopRightRadius: 18 },
  itemName: { color: '#fff', fontSize: 22, fontWeight: '700' },
  itemSub: { color: '#c5d0db', fontSize: 17 },
  sku: { color: '#8394a5', fontSize: 13 },
  stock: { color: '#6fdc8c', fontSize: 20, fontWeight: '700', marginTop: 4 },
  stockLow: { color: '#ffb454' },
  stepper: { flexDirection: 'row', alignItems: 'center', justifyContent: 'center', gap: 14,
    marginVertical: 6 },
  stepBtn: { width: 56, height: 56, borderRadius: 28, backgroundColor: '#25303b',
    alignItems: 'center', justifyContent: 'center' },
  stepText: { color: '#fff', fontSize: 28, fontWeight: '600' },
  qtyInput: { minWidth: 90, textAlign: 'center', color: '#fff', fontSize: 30, fontWeight: '700',
    backgroundColor: '#0f1419', borderRadius: 12, paddingVertical: 8 },
  btn: { paddingVertical: 14, borderRadius: 12, alignItems: 'center' },
  btnPrimary: { backgroundColor: '#2f81f7' },
  btnGhost: { backgroundColor: '#25303b' },
  btnDisabled: { opacity: 0.4 },
  btnText: { color: '#fff', fontSize: 17, fontWeight: '600' },
  banner: { padding: 10, borderRadius: 10, overflow: 'hidden', fontSize: 15, fontWeight: '600' },
  bannerOk: { backgroundColor: '#17381f', color: '#8fe6a5' },
  bannerErr: { backgroundColor: '#41191b', color: '#ff9b9f' },
  settingsBody: { padding: 20, gap: 10 },
  h1: { color: '#fff', fontSize: 26, fontWeight: '700', marginBottom: 6 },
  label: { color: '#9fb0c0', fontSize: 14, marginTop: 8 },
  muted: { color: '#9fb0c0', fontSize: 15, textAlign: 'center' },
  input: { backgroundColor: '#1e2730', color: '#fff', borderRadius: 10, padding: 12, fontSize: 16 },
});
