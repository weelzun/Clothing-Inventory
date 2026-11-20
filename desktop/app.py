"""
Clothing Inventory: desktop app (PySide6).

Run from the repository root with the virtual environment active:
    python desktop/app.py

It connects straight to MySQL. If the MYSQL_PASSWORD environment variable is set,
the password box is pre-filled with it.
"""

import functools
import os
import re
import sys
from pathlib import Path

import mysql.connector
from PySide6.QtCore import QLocale, QSettings, Qt, QTimer, QUrl
from PySide6.QtGui import QColor, QDesktopServices, QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QAbstractItemView, QApplication, QCheckBox, QComboBox, QDialog, QDialogButtonBox,
    QFileDialog, QFormLayout, QHBoxLayout, QHeaderView, QInputDialog, QLabel, QLineEdit,
    QListWidget, QListWidgetItem, QMainWindow, QMessageBox, QPushButton, QSpinBox,
    QTableWidget, QTableWidgetItem, QTabWidget, QVBoxLayout, QWidget,
)

from db import (
    MOVEMENT_TYPES, STYLE_RE, Database, ValidationError, make_sku,
)

# The label generator lives in ../labels/make_labels.py and is reused here.
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "labels"))
from make_labels import build_pdf  # noqa: E402

SETTINGS = ("ClothingInventory", "Desktop")
GENDERS = ["unisex", "men", "women", "kids"]
LOW_COLOR = "#d9822b"
ZERO_COLOR = "#d64545"
GOOD_COLOR = "#2e9d57"


# ----------------------------------------------------------------------
# helpers
# ----------------------------------------------------------------------
def peso(value):
    """100000 -> $100.000 (Colombian format, no decimals)."""
    return "$" + f"{round(float(value)):,}".replace(",", ".")


def peso_spin(maximum=99_999_999):
    spin = QSpinBox()
    spin.setRange(0, maximum)
    spin.setPrefix("$ ")
    spin.setSingleStep(1000)
    spin.setGroupSeparatorShown(True)
    spin.setLocale(QLocale(QLocale.Language.Spanish, QLocale.Country.Colombia))
    return spin


def guarded(fn):
    """Show friendly dialogs instead of crashing when something goes wrong."""
    @functools.wraps(fn)
    def wrapper(self, *_):
        try:
            return fn(self)
        except ValidationError as e:
            QMessageBox.warning(self, "Check your input", str(e))
        except mysql.connector.Error as e:
            QMessageBox.critical(self, "Database error", e.msg or str(e))
        except Exception as e:  # noqa: BLE001
            QMessageBox.critical(self, "Error", f"{type(e).__name__}: {e}")
    return wrapper


class NumItem(QTableWidgetItem):
    """Table cell that sorts by a number even though it shows formatted text."""
    def __init__(self, text, key):
        super().__init__(text)
        self._key = key

    def __lt__(self, other):
        if isinstance(other, NumItem):
            return self._key < other._key
        return super().__lt__(other)


def cell(text, key=None, right=False, color=None):
    item = NumItem(text, key) if key is not None else QTableWidgetItem(text)
    if right:
        item.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
    if color:
        item.setForeground(QColor(color))
    return item


def make_table(headers, stretch=None):
    """`stretch` = index of the one column that should absorb extra width."""
    table = QTableWidget(0, len(headers))
    table.setHorizontalHeaderLabels(headers)
    table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
    table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
    table.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
    table.verticalHeader().setVisible(False)
    table.setAlternatingRowColors(True)
    table.horizontalHeader().setSortIndicator(-1, Qt.SortOrder.AscendingOrder)
    header = table.horizontalHeader()
    header.setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
    header.setStretchLastSection(False)
    if stretch is not None:
        header.setSectionResizeMode(stretch, QHeaderView.ResizeMode.Stretch)
    return table


def fill_table(table, rows):
    table.setSortingEnabled(False)
    table.setRowCount(len(rows))
    for r, items in enumerate(rows):
        for c, item in enumerate(items):
            table.setItem(r, c, item)
    table.setSortingEnabled(True)


def combo_choice(combo):
    """Editable combo: returns the item's data, a new typed name, or None."""
    text = combo.currentText().strip()
    if not text:
        return None
    idx = combo.findText(text, Qt.MatchFlag.MatchFixedString)  # case-insensitive
    if idx >= 0:
        return combo.itemData(idx) if combo.itemData(idx) is not None else combo.itemText(idx)
    return text


def label_row(v):
    """Shape a variant the way make_labels.build_pdf expects."""
    return {
        "name": v["product_name"], "color": v["color"], "size": v["size"],
        "sku": v["sku"], "barcode": v["barcode"], "price": v["retail_price"],
    }


# ----------------------------------------------------------------------
# connect dialog
# ----------------------------------------------------------------------
class LoginDialog(QDialog):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Connect to the inventory database")
        s = QSettings(*SETTINGS)
        self.host = QLineEdit(str(s.value("host", "localhost")))
        self.port = QLineEdit(str(s.value("port", "3306")))
        self.user = QLineEdit(str(s.value("user", "root")))
        self.password = QLineEdit(os.environ.get("MYSQL_PASSWORD", ""))
        self.password.setEchoMode(QLineEdit.EchoMode.Password)

        form = QFormLayout()
        form.addRow("Server", self.host)
        form.addRow("Port", self.port)
        form.addRow("User", self.user)
        form.addRow("Password", self.password)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText("Connect")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.addLayout(form)
        layout.addWidget(buttons)
        self.setMinimumWidth(360)
        (self.password if self.user.text() else self.user).setFocus()

    def values(self):
        return self.host.text().strip(), int(self.port.text().strip()), self.user.text().strip(), self.password.text()

    def remember(self):
        s = QSettings(*SETTINGS)
        s.setValue("host", self.host.text().strip())
        s.setValue("port", self.port.text().strip())
        s.setValue("user", self.user.text().strip())


# ----------------------------------------------------------------------
# New product
# ----------------------------------------------------------------------
class AddProductDialog(QDialog):
    def __init__(self, db, parent=None):
        super().__init__(parent)
        self.db = db
        self.existing = None
        self.outcome = None  # (product_was_created, number_of_new_variants)
        self.setWindowTitle("New product")
        self.resize(660, 700)

        self.style_code = QLineEdit()
        self.style_code.setPlaceholderText("e.g. CT-001 (letters, digits, hyphens)")
        self.style_code.editingFinished.connect(self.check_existing)
        self.style_code.textChanged.connect(self.update_preview)
        self.note = QLabel("")
        self.note.setWordWrap(True)
        self.note.setStyleSheet(f"color: {LOW_COLOR};")

        self.name = QLineEdit()
        self.brand = QComboBox()
        self.brand.setEditable(True)
        self.brand.addItem("")
        self.brand.addItems(db.brands())
        self.brand.lineEdit().setPlaceholderText("optional: pick one or type a new brand")
        self.category = QComboBox()
        self.category.setEditable(True)
        self.category.addItem("", None)
        for cid, path in db.categories():
            self.category.addItem(path, cid)
        self.category.lineEdit().setPlaceholderText("optional: pick one or type a new category")
        self.gender = QComboBox()
        self.gender.addItems(GENDERS)
        self.cost = peso_spin()
        self.price = peso_spin()

        form = QFormLayout()
        form.addRow("Style code", self.style_code)
        form.addRow("", self.note)
        form.addRow("Name", self.name)
        form.addRow("Brand", self.brand)
        form.addRow("Category", self.category)
        form.addRow("Gender", self.gender)
        form.addRow("Cost", self.cost)
        form.addRow("Price", self.price)
        self.product_fields = (self.name, self.brand, self.category, self.gender, self.cost, self.price)

        self.colors = QListWidget()
        self.sizes = QListWidget()
        self.colors.itemChanged.connect(self.update_preview)
        self.sizes.itemChanged.connect(self.update_preview)
        self.load_colors()
        self.load_sizes()

        new_color = QPushButton("+ New color…")
        new_color.clicked.connect(self.on_new_color)
        new_size = QPushButton("+ New size…")
        new_size.clicked.connect(self.on_new_size)
        col_box = QVBoxLayout()
        col_box.addWidget(QLabel("Colors"))
        col_box.addWidget(self.colors)
        col_box.addWidget(new_color)
        size_box = QVBoxLayout()
        size_box.addWidget(QLabel("Sizes"))
        size_box.addWidget(self.sizes)
        size_box.addWidget(new_size)
        lists = QHBoxLayout()
        lists.addLayout(col_box)
        lists.addLayout(size_box)

        self.preview = QLabel("")
        self.preview.setWordWrap(True)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText("Create")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.addLayout(form)
        layout.addLayout(lists, 1)
        layout.addWidget(self.preview)
        layout.addWidget(buttons)
        self.update_preview()

    # -- lists ---------------------------------------------------------
    def _checked_ids(self, widget, key):
        out = set()
        for i in range(widget.count()):
            it = widget.item(i)
            if it.checkState() == Qt.CheckState.Checked:
                out.add(it.data(Qt.ItemDataRole.UserRole)[key])
        return out

    def _add_check_item(self, widget, text, data, checked):
        it = QListWidgetItem(text)
        it.setFlags(it.flags() | Qt.ItemFlag.ItemIsUserCheckable)
        it.setCheckState(Qt.CheckState.Checked if checked else Qt.CheckState.Unchecked)
        it.setData(Qt.ItemDataRole.UserRole, data)
        widget.addItem(it)

    def load_colors(self, tick=None):
        keep = self._checked_ids(self.colors, "color_id")
        self.colors.blockSignals(True)
        self.colors.clear()
        for c in self.db.colors():
            self._add_check_item(self.colors, f"{c['name']}  ({c['code']})", c,
                                 c["color_id"] in keep or c["color_id"] == tick)
        self.colors.blockSignals(False)

    def load_sizes(self, tick=None):
        keep = self._checked_ids(self.sizes, "size_id")
        self.sizes.blockSignals(True)
        self.sizes.clear()
        for s in self.db.sizes():
            self._add_check_item(self.sizes, s["label"], s,
                                 s["size_id"] in keep or s["size_id"] == tick)
        self.sizes.blockSignals(False)

    def _checked(self, widget):
        return [widget.item(i).data(Qt.ItemDataRole.UserRole)
                for i in range(widget.count())
                if widget.item(i).checkState() == Qt.CheckState.Checked]

    @guarded
    def on_new_color(self):
        name, ok = QInputDialog.getText(self, "New color", "Color name (e.g. Burgundy):")
        if not ok or not name.strip():
            return
        suggestion = re.sub(r"[^A-Za-z0-9]", "", name).upper()[:3]
        code, ok = QInputDialog.getText(
            self, "New color", "Short code for SKUs (1-5 letters/digits):", text=suggestion)
        if not ok:
            return
        new_id = self.db.add_color(name, code)
        self.load_colors(tick=new_id)
        self.update_preview()

    @guarded
    def on_new_size(self):
        label, ok = QInputDialog.getText(self, "New size", "Size label (e.g. 3XL, 38, One Size):")
        if not ok or not label.strip():
            return
        order, ok = QInputDialog.getInt(
            self, "New size", "Position in the size order (smaller number = earlier):",
            self.db.next_size_order(), 0, 100000, 1)
        if not ok:
            return
        new_id = self.db.add_size(label, order)
        self.load_sizes(tick=new_id)
        self.update_preview()

    # -- behaviour -----------------------------------------------------
    def check_existing(self):
        style = self.style_code.text().strip().upper()
        product = self.db.product_by_style(style) if style else None
        self.existing = product
        for w in self.product_fields:
            w.setEnabled(product is None)
        if product:
            self.name.setText(product["name"])
            self.brand.setCurrentText(product["brand"] or "")
            idx = self.category.findData(product["category_id"])
            self.category.setCurrentIndex(max(idx, 0))
            self.gender.setCurrentText(product["gender"])
            self.cost.setValue(int(product["base_cost"]))
            self.price.setValue(int(product["base_price"]))
            self.note.setText("This style already exists. Only the colors/sizes you tick "
                              "that it doesn't have yet will be added.")
        else:
            self.note.setText("")
        self.update_preview()

    def update_preview(self, *_):
        style = self.style_code.text().strip().upper()
        colors, sizes = self._checked(self.colors), self._checked(self.sizes)
        if colors and sizes and style:
            example = make_sku(style, colors[0]["code"], sizes[0]["label"])
            self.preview.setText(
                f"{len(colors) * len(sizes)} variant(s) selected. Example SKU and barcode: {example}")
        else:
            self.preview.setText("Enter a style code and tick at least one color and one size.")

    @guarded
    def accept(self):
        style = self.style_code.text().strip().upper()
        if not STYLE_RE.match(style):
            raise ValidationError("Enter a style code using only letters, digits and hyphens.")
        colors, sizes = self._checked(self.colors), self._checked(self.sizes)
        if not colors or not sizes:
            raise ValidationError("Tick at least one color and one size.")
        if self.existing is None:
            self.existing = self.db.product_by_style(style)  # in case editing wasn't "finished"
        created, new_ids = self.db.add_product(
            style, self.name.text(), self.brand.currentText(), combo_choice(self.category),
            self.gender.currentText(), self.cost.value(), self.price.value(),
            [c["color_id"] for c in colors], [s["size_id"] for s in sizes],
        )
        if not created and not new_ids:
            QMessageBox.information(self, "Nothing to add",
                                    "Every selected color/size already exists for this style.")
            return
        self.outcome = (created, len(new_ids))
        super().accept()


# ----------------------------------------------------------------------
# Receive a delivery (all sizes/colors of one style at once)
# ----------------------------------------------------------------------
class ReceiveDialog(QDialog):
    def __init__(self, db, variants, parent=None):
        super().__init__(parent)
        self.db = db
        self.variants = variants
        self.received = []  # [(variant, qty)]
        self.setWindowTitle("Receive delivery")
        self.resize(680, 560)

        header = QLabel(f"<b>{variants[0]['product_name']}</b>  ({variants[0]['style_code']})")

        self.table = make_table(["Color", "Size", "SKU", "In stock", "Receive"], stretch=2)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        self.table.setRowCount(len(variants))
        self.spins = []
        for r, v in enumerate(variants):
            self.table.setItem(r, 0, cell(v["color"]))
            self.table.setItem(r, 1, cell(v["size"]))
            self.table.setItem(r, 2, cell(v["sku"]))
            self.table.setItem(r, 3, cell(str(v["quantity"]), right=True))
            spin = QSpinBox()
            spin.setRange(0, 100000)
            self.table.setCellWidget(r, 4, spin)
            self.spins.append(spin)

        self.fill_value = QSpinBox()
        self.fill_value.setRange(0, 100000)
        self.fill_value.setValue(1)
        fill_btn = QPushButton("Set every row")
        fill_btn.clicked.connect(lambda: [s.setValue(self.fill_value.value()) for s in self.spins])
        fill_row = QHBoxLayout()
        fill_row.addWidget(QLabel("Quick fill:"))
        fill_row.addWidget(self.fill_value)
        fill_row.addWidget(fill_btn)
        fill_row.addStretch(1)

        self.reference = QLineEdit()
        self.reference.setPlaceholderText("optional: invoice or purchase order number")
        self.print_cb = QCheckBox("Print labels for the received units afterwards")
        self.print_cb.setChecked(True)

        form = QFormLayout()
        form.addRow("Reference", self.reference)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText("Receive")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.addWidget(header)
        layout.addWidget(self.table, 1)
        layout.addLayout(fill_row)
        layout.addLayout(form)
        layout.addWidget(self.print_cb)
        layout.addWidget(buttons)

    @guarded
    def accept(self):
        todo = [(v, s.value()) for v, s in zip(self.variants, self.spins) if s.value() > 0]
        if not todo:
            raise ValidationError("Enter a quantity for at least one row.")
        done = []
        try:
            for v, qty in todo:
                self.db.adjust(v["variant_id"], "RECEIVE", qty, self.reference.text().strip())
                done.append((v, qty))
        except Exception as e:  # noqa: BLE001
            msg = e.msg if isinstance(e, mysql.connector.Error) and e.msg else str(e)
            QMessageBox.critical(
                self, "Stopped partway",
                f"{msg}\n\n{len(done)} of {len(todo)} rows were saved before the error. "
                "Check the movement history before trying again.")
            if not done:
                return
        self.received = done
        super().accept()


# ----------------------------------------------------------------------
# Adjust one item
# ----------------------------------------------------------------------
class AdjustDialog(QDialog):
    OPS = [
        ("Receive (adds stock)", "RECEIVE"), ("Customer return (adds stock)", "RETURN"),
        ("Sale (removes stock)", "SALE"), ("Damaged (removes stock)", "DAMAGED"),
        ("Lost (removes stock)", "LOST"), ("Set exact count (stocktake)", "COUNT"),
    ]

    def __init__(self, db, variant, parent=None):
        super().__init__(parent)
        self.db = db
        self.v = variant
        self.setWindowTitle("Adjust stock")
        self.setMinimumWidth(420)

        header = QLabel(
            f"<b>{variant['product_name']}</b><br>{variant['color']} / {variant['size']}"
            f" &nbsp;·&nbsp; {variant['sku']}<br>Currently in stock: <b>{variant['quantity']}</b>")

        self.op = QComboBox()
        for text, code in self.OPS:
            self.op.addItem(text, code)
        self.qty_label = QLabel("Quantity")
        self.qty = QSpinBox()
        self.qty.setRange(1, 100000)
        self.reference = QLineEdit()
        self.notes = QLineEdit()
        self.op.currentIndexChanged.connect(self.on_op_changed)

        form = QFormLayout()
        form.addRow("Action", self.op)
        form.addRow(self.qty_label, self.qty)
        form.addRow("Reference", self.reference)
        form.addRow("Notes", self.notes)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText("Apply")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.addWidget(header)
        layout.addLayout(form)
        layout.addWidget(buttons)

    def on_op_changed(self):
        if self.op.currentData() == "COUNT":
            self.qty_label.setText("Counted quantity")
            self.qty.setRange(0, 100000)
            self.qty.setValue(self.v["quantity"])
        else:
            self.qty_label.setText("Quantity")
            self.qty.setRange(1, 100000)
            self.qty.setValue(1)

    @guarded
    def accept(self):
        code = self.op.currentData()
        if code == "COUNT":
            self.db.set_count(self.v["variant_id"], self.qty.value(), self.notes.text().strip())
        else:
            self.db.adjust(self.v["variant_id"], code, self.qty.value(),
                           self.reference.text().strip(), self.notes.text().strip())
        super().accept()


# ----------------------------------------------------------------------
# Edit product
# ----------------------------------------------------------------------
class EditProductDialog(QDialog):
    def __init__(self, db, product, parent=None):
        super().__init__(parent)
        self.db = db
        self.product = product
        self.setWindowTitle("Edit product")
        self.setMinimumWidth(420)

        self.name = QLineEdit(product["name"])
        self.cost = peso_spin()
        self.cost.setValue(int(product["base_cost"]))
        self.price = peso_spin()
        self.price.setValue(int(product["base_price"]))
        self.active = QCheckBox("Active (untick to hide this product and all its sizes/colors)")
        self.active.setChecked(bool(product["is_active"]))

        form = QFormLayout()
        form.addRow("Style code", QLabel(product["style_code"]))
        form.addRow("Name", self.name)
        form.addRow("Cost", self.cost)
        form.addRow("Price", self.price)
        form.addRow("", self.active)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.addLayout(form)
        layout.addWidget(buttons)

    @guarded
    def accept(self):
        self.db.edit_product(self.product["product_id"], self.name.text(), self.cost.value(),
                             self.price.value(), self.active.isChecked())
        super().accept()


# ----------------------------------------------------------------------
# Print labels
# ----------------------------------------------------------------------
class PrintLabelsDialog(QDialog):
    MODES = [("Sheet: Avery 5160, 30 labels per page", "sheet"),
             ("Roll: one label per page (thermal printer)", "roll")]

    def __init__(self, db, variants, copies=None, parent=None):
        super().__init__(parent)
        self.db = db
        self.variants = variants
        self.setWindowTitle("Print labels")
        self.resize(720, 520)
        copies = copies or {}

        self.table = make_table(["Product", "Color", "Size", "SKU", "Price", "Labels"], stretch=0)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        self.table.setRowCount(len(variants))
        self.spins = []
        for r, v in enumerate(variants):
            for c, text in enumerate([v["product_name"], v["color"], v["size"], v["sku"],
                                      peso(v["retail_price"])]):
                self.table.setItem(r, c, cell(text))
            spin = QSpinBox()
            spin.setRange(0, 5000)
            spin.setValue(copies.get(v["variant_id"], 1))
            spin.valueChanged.connect(self.update_total)
            self.table.setCellWidget(r, 5, spin)
            self.spins.append(spin)

        self.mode = QComboBox()
        for text, code in self.MODES:
            self.mode.addItem(text, code)
        saved = QSettings(*SETTINGS).value("label_mode", "sheet")
        self.mode.setCurrentIndex(max(self.mode.findData(saved), 0))

        self.total = QLabel("")
        form = QFormLayout()
        form.addRow("Layout", self.mode)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText("Create PDF…")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.addWidget(QLabel("Choose how many labels to print for each item."))
        layout.addWidget(self.table, 1)
        layout.addLayout(form)
        layout.addWidget(self.total)
        layout.addWidget(buttons)
        self.update_total()

    def update_total(self, *_):
        self.total.setText(f"Total labels: {sum(s.value() for s in self.spins)}")

    def counts(self):
        return [(v, s.value()) for v, s in zip(self.variants, self.spins) if s.value() > 0]

    def generate(self, path):
        """Write the PDF and log the print job. Returns the number of labels."""
        counts = self.counts()
        if not counts:
            raise ValidationError("Set at least one label to print.")
        rows = [label_row(v) for v, n in counts for _ in range(n)]
        total = build_pdf(rows, 1, self.mode.currentData(), str(path))
        for v, n in counts:
            self.db.log_label_print(v["variant_id"], n)
        return total

    @guarded
    def accept(self):
        if not self.counts():
            raise ValidationError("Set at least one label to print.")
        settings = QSettings(*SETTINGS)
        start = Path(str(settings.value("label_dir", str(Path.home() / "Documents")))) / "labels.pdf"
        path, _ = QFileDialog.getSaveFileName(self, "Save labels as", str(start), "PDF files (*.pdf)")
        if not path:
            return
        if not path.lower().endswith(".pdf"):
            path += ".pdf"
        self.generate(path)
        settings.setValue("label_mode", self.mode.currentData())
        settings.setValue("label_dir", str(Path(path).parent))
        QDesktopServices.openUrl(QUrl.fromLocalFile(path))  # opens in your PDF viewer to print
        super().accept()


# ----------------------------------------------------------------------
# Main window
# ----------------------------------------------------------------------
class MainWindow(QMainWindow):
    INV_HEADERS = ["Style", "Product", "Brand", "Category", "Color", "Size", "SKU",
                   "In stock", "Price", "Reorder at"]
    HIST_HEADERS = ["When", "SKU", "Product", "Type", "Change", "After", "User",
                    "Source", "Reference", "Notes"]

    def __init__(self, db):
        super().__init__()
        self.db = db
        self.by_vid = {}
        self.setWindowTitle("Clothing Inventory")
        self.resize(1250, 740)

        tabs = QTabWidget()
        tabs.addTab(self._build_inventory_tab(), "Inventory")
        tabs.addTab(self._build_history_tab(), "Movement history")
        tabs.currentChanged.connect(self.on_tab_changed)
        self.tabs = tabs
        self.setCentralWidget(tabs)

        self.status_label = QLabel("")
        self.statusBar().addWidget(self.status_label, 1)
        self.statusBar().addPermanentWidget(QLabel(f"{db.who}  ·  {db.location_name}"))

        QShortcut(QKeySequence("F5"), self, activated=self.refresh_all)
        QShortcut(QKeySequence("Ctrl+F"), self, activated=lambda: self.search.setFocus())
        self.refresh_all()

    # -- inventory tab -------------------------------------------------
    def _build_inventory_tab(self):
        self.search = QLineEdit()
        self.search.setPlaceholderText("Search name, SKU, barcode, color, size, brand…  (Ctrl+F)")
        self.search.setClearButtonEnabled(True)
        self._search_timer = QTimer(self, singleShot=True, interval=250)
        self._search_timer.timeout.connect(self.refresh_inventory)
        self.search.textChanged.connect(lambda: self._search_timer.start())
        self.low = QCheckBox("Low stock only")
        self.low.toggled.connect(lambda: self.refresh_inventory())
        self.inactive = QCheckBox("Show inactive")
        self.inactive.toggled.connect(lambda: self.refresh_inventory())
        refresh = QPushButton("Refresh")
        refresh.clicked.connect(lambda: self.refresh_all())

        top = QHBoxLayout()
        top.addWidget(self.search, 1)
        top.addWidget(self.low)
        top.addWidget(self.inactive)
        top.addWidget(refresh)

        self.table = make_table(self.INV_HEADERS, stretch=1)
        self.table.doubleClicked.connect(lambda *_: self.on_adjust())

        def button(text, slot):
            b = QPushButton(text)
            b.clicked.connect(lambda: slot())
            return b

        bottom = QHBoxLayout()
        bottom.addWidget(button("New product…", self.on_new_product))
        bottom.addWidget(button("Receive delivery…", self.on_receive))
        bottom.addWidget(button("Adjust stock…", self.on_adjust))
        bottom.addWidget(button("Edit product…", self.on_edit))
        bottom.addStretch(1)
        bottom.addWidget(button("Print labels…", self.on_print))

        page = QWidget()
        layout = QVBoxLayout(page)
        layout.addLayout(top)
        layout.addWidget(self.table, 1)
        layout.addLayout(bottom)
        return page

    # -- history tab ---------------------------------------------------
    def _build_history_tab(self):
        self.h_search = QLineEdit()
        self.h_search.setPlaceholderText("Search SKU, product, reference, notes…")
        self.h_search.setClearButtonEnabled(True)
        self._h_timer = QTimer(self, singleShot=True, interval=250)
        self._h_timer.timeout.connect(self.refresh_history)
        self.h_search.textChanged.connect(lambda: self._h_timer.start())
        self.h_type = QComboBox()
        self.h_type.addItem("All types", None)
        for t in MOVEMENT_TYPES:
            self.h_type.addItem(t.title().replace("_", " "), t)
        self.h_type.currentIndexChanged.connect(lambda: self.refresh_history())

        top = QHBoxLayout()
        top.addWidget(self.h_search, 1)
        top.addWidget(self.h_type)
        self.h_note = QLabel("Showing the latest 500 movements.")

        self.h_table = make_table(self.HIST_HEADERS, stretch=9)
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.addLayout(top)
        layout.addWidget(self.h_table, 1)
        layout.addWidget(self.h_note)
        return page

    def on_tab_changed(self, index):
        if index == 1:
            self.refresh_history()

    # -- refreshing ----------------------------------------------------
    @guarded
    def refresh_all(self):
        self.refresh_inventory()
        if self.tabs.currentIndex() == 1:
            self.refresh_history()

    @guarded
    def refresh_inventory(self):
        rows = self.db.inventory(self.search.text(), self.low.isChecked(), self.inactive.isChecked())
        self.by_vid = {r["variant_id"]: r for r in rows}
        out = []
        for r in rows:
            qty, reorder = r["quantity"], r["reorder_level"]
            color = ZERO_COLOR if qty <= 0 else (LOW_COLOR if qty <= reorder else None)
            items = [
                cell(r["style_code"]), cell(r["product_name"]), cell(r["brand"] or ""),
                cell(r["category"] or ""), cell(r["color"]), cell(r["size"]), cell(r["sku"]),
                cell(str(qty), key=qty, right=True, color=color),
                cell(peso(r["retail_price"]), key=r["retail_price"], right=True),
                cell(str(reorder), key=reorder, right=True),
            ]
            items[0].setData(Qt.ItemDataRole.UserRole, r["variant_id"])
            if not r["is_active"]:
                for it in items:
                    it.setForeground(QColor("#888888"))
            out.append(items)
        fill_table(self.table, out)

        s = self.db.stats()
        self.status_label.setText(
            f"{s['variants']} items  ·  {s['units']} units in stock  ·  "
            f"{s['low']} low or out of stock  ·  stock value at cost {peso(s['value'])}")

    @guarded
    def refresh_history(self):
        rows = self.db.movements(self.h_search.text(), self.h_type.currentData())
        out = []
        for r in rows:
            change = int(r["quantity_change"])
            items = [
                cell(str(r["created_at"])[:19]), cell(r["sku"]), cell(r["product_name"]),
                cell(r["movement_type"].title().replace("_", " ")),
                cell(f"{change:+d}", key=change, right=True,
                     color=GOOD_COLOR if change > 0 else ZERO_COLOR),
                cell(str(r["quantity_after"]), key=int(r["quantity_after"]), right=True),
                cell(r["username"]), cell(r["source"]), cell(r["reference"] or ""),
                cell(r["notes"] or ""),
            ]
            out.append(items)
        fill_table(self.h_table, out)

    # -- selection helpers ---------------------------------------------
    def selected(self):
        rows = sorted({i.row() for i in self.table.selectedIndexes()})
        return [self.by_vid[self.table.item(r, 0).data(Qt.ItemDataRole.UserRole)] for r in rows]

    def need_one(self):
        sel = self.selected()
        if len(sel) != 1:
            raise ValidationError("Select one row in the table first.")
        return sel[0]

    # -- actions -------------------------------------------------------
    @guarded
    def on_new_product(self):
        dlg = AddProductDialog(self.db, self)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            created, n = dlg.outcome
            self.refresh_inventory()
            what = "Product created" if created else "Variants added to the existing product"
            QMessageBox.information(
                self, "Done",
                f"{what}: {n} new item(s). Stock starts at 0. Use \"Receive delivery…\" "
                "to add stock and print labels.")

    @guarded
    def on_receive(self):
        sel = self.selected()
        if not sel:
            raise ValidationError("Select any row of the product you're receiving first.")
        product_id = sel[0]["product_id"]
        variants = self.db.inventory(product_id=product_id)
        dlg = ReceiveDialog(self.db, variants, self)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        received = dlg.received
        self.refresh_inventory()
        if received and dlg.print_cb.isChecked():
            fresh = {v["variant_id"]: v for v in self.db.inventory(product_id=product_id)}
            items = [fresh[v["variant_id"]] for v, _ in received]
            copies = {v["variant_id"]: q for v, q in received}
            PrintLabelsDialog(self.db, items, copies, self).exec()
            self.refresh_inventory()

    @guarded
    def on_adjust(self):
        variant = self.need_one()
        if AdjustDialog(self.db, variant, self).exec() == QDialog.DialogCode.Accepted:
            self.refresh_inventory()

    @guarded
    def on_edit(self):
        variant = self.need_one()
        product = self.db.product_by_style(variant["style_code"])
        if EditProductDialog(self.db, product, self).exec() == QDialog.DialogCode.Accepted:
            self.refresh_inventory()

    @guarded
    def on_print(self):
        sel = self.selected()
        if not sel:
            raise ValidationError("Select one or more rows first (Ctrl+A selects all shown).")
        PrintLabelsDialog(self.db, sel, None, self).exec()
        self.refresh_inventory()


# ----------------------------------------------------------------------
def main():
    app = QApplication(sys.argv)
    app.setApplicationName("Clothing Inventory")
    while True:
        dlg = LoginDialog()
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return 0
        try:
            host, port, user, password = dlg.values()
            db = Database(host, port, user, password)
        except Exception as e:  # noqa: BLE001
            msg = e.msg if isinstance(e, mysql.connector.Error) and e.msg else str(e)
            QMessageBox.critical(None, "Could not connect", msg)
            continue
        dlg.remember()
        break
    window = MainWindow(db)
    window.show()
    code = app.exec()
    db.close()
    return code


if __name__ == "__main__":
    sys.exit(main())
