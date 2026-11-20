-- =====================================================================
-- Clothing Store Inventory Schema
-- Target: MySQL 8.0.16+ (CHECK constraints are enforced from 8.0.16)
-- Run in DBeaver with "Execute SQL Script" (Alt+X), not single statement.
-- =====================================================================

DROP DATABASE IF EXISTS clothing_inventory;
CREATE DATABASE clothing_inventory
  CHARACTER SET utf8mb4
  COLLATE utf8mb4_0900_ai_ci;
USE clothing_inventory;

-- ---------------------------------------------------------------------
-- USERS (who performed each stock action)
-- ---------------------------------------------------------------------
CREATE TABLE users (
  user_id        INT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
  username       VARCHAR(50)  NOT NULL UNIQUE,
  password_hash  VARCHAR(255) NOT NULL,          -- store bcrypt/argon2 hashes only
  full_name      VARCHAR(100) NOT NULL,
  role           ENUM('admin','manager','staff') NOT NULL DEFAULT 'staff',
  is_active      TINYINT(1)   NOT NULL DEFAULT 1,
  created_at     TIMESTAMP    NOT NULL DEFAULT CURRENT_TIMESTAMP
) ENGINE=InnoDB;

-- ---------------------------------------------------------------------
-- LOOKUP TABLES
-- ---------------------------------------------------------------------
CREATE TABLE brands (
  brand_id  INT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
  name      VARCHAR(100) NOT NULL UNIQUE
) ENGINE=InnoDB;

CREATE TABLE categories (
  category_id  INT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
  parent_id    INT UNSIGNED NULL,                -- e.g. Tops > T-Shirts
  name         VARCHAR(100) NOT NULL,
  UNIQUE KEY uq_category (parent_id, name),
  CONSTRAINT fk_category_parent FOREIGN KEY (parent_id)
    REFERENCES categories(category_id) ON DELETE SET NULL
) ENGINE=InnoDB;

CREATE TABLE suppliers (
  supplier_id    INT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
  name           VARCHAR(150) NOT NULL UNIQUE,
  contact_name   VARCHAR(100),
  email          VARCHAR(150),
  phone          VARCHAR(40),
  address        VARCHAR(255),
  is_active      TINYINT(1) NOT NULL DEFAULT 1
) ENGINE=InnoDB;

CREATE TABLE colors (
  color_id  INT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
  name      VARCHAR(50) NOT NULL UNIQUE,
  code      VARCHAR(5)  NOT NULL UNIQUE,         -- short code used in SKUs, e.g. BLK
  hex_value CHAR(7) NULL                         -- optional, e.g. #000000
) ENGINE=InnoDB;

CREATE TABLE sizes (
  size_id     INT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
  label       VARCHAR(20) NOT NULL UNIQUE,       -- S, M, L, 32, 10, One Size
  sort_order  SMALLINT NOT NULL DEFAULT 0        -- so XS < S < M < L displays correctly
) ENGINE=InnoDB;

-- ---------------------------------------------------------------------
-- LOCATIONS (one row for a single store; lets you grow later)
-- ---------------------------------------------------------------------
CREATE TABLE locations (
  location_id  INT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
  name         VARCHAR(100) NOT NULL UNIQUE,
  type         ENUM('store','stockroom','warehouse') NOT NULL DEFAULT 'store',
  is_active    TINYINT(1) NOT NULL DEFAULT 1
) ENGINE=InnoDB;

-- ---------------------------------------------------------------------
-- PRODUCTS (the "style": e.g. Classic Tee) and VARIANTS (style+color+size)
-- ---------------------------------------------------------------------
CREATE TABLE products (
  product_id    INT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
  style_code    VARCHAR(30)  NOT NULL UNIQUE,    -- used as SKU prefix, e.g. CT-001
  name          VARCHAR(150) NOT NULL,
  description   TEXT,
  brand_id      INT UNSIGNED NULL,
  category_id   INT UNSIGNED NULL,
  supplier_id   INT UNSIGNED NULL,
  gender        ENUM('men','women','unisex','kids') NOT NULL DEFAULT 'unisex',
  season        VARCHAR(30) NULL,                -- e.g. SS26
  base_cost     DECIMAL(10,2) NOT NULL DEFAULT 0.00,
  base_price    DECIMAL(10,2) NOT NULL DEFAULT 0.00,
  is_active     TINYINT(1) NOT NULL DEFAULT 1,
  created_at    TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at    TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  CONSTRAINT chk_prod_money CHECK (base_cost >= 0 AND base_price >= 0),
  CONSTRAINT fk_prod_brand    FOREIGN KEY (brand_id)    REFERENCES brands(brand_id)       ON DELETE SET NULL,
  CONSTRAINT fk_prod_category FOREIGN KEY (category_id) REFERENCES categories(category_id) ON DELETE SET NULL,
  CONSTRAINT fk_prod_supplier FOREIGN KEY (supplier_id) REFERENCES suppliers(supplier_id)  ON DELETE SET NULL,
  INDEX idx_prod_name (name)
) ENGINE=InnoDB;

CREATE TABLE product_variants (
  variant_id     INT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
  product_id     INT UNSIGNED NOT NULL,
  color_id       INT UNSIGNED NOT NULL,
  size_id        INT UNSIGNED NOT NULL,
  sku            VARCHAR(50) NOT NULL UNIQUE,    -- internal stock keeping unit
  barcode        VARCHAR(48) NOT NULL UNIQUE,    -- value encoded in the printed barcode
  barcode_type   ENUM('CODE128','EAN13','UPCA','QR') NOT NULL DEFAULT 'CODE128',
  cost_price     DECIMAL(10,2) NULL,             -- NULL = use product base_cost
  retail_price   DECIMAL(10,2) NULL,             -- NULL = use product base_price
  reorder_level  INT UNSIGNED NOT NULL DEFAULT 2, -- alert when stock <= this
  reorder_qty    INT UNSIGNED NOT NULL DEFAULT 6,
  is_active      TINYINT(1) NOT NULL DEFAULT 1,
  created_at     TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at     TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  UNIQUE KEY uq_variant_combo (product_id, color_id, size_id),
  CONSTRAINT chk_var_money CHECK (
    (cost_price IS NULL OR cost_price >= 0) AND (retail_price IS NULL OR retail_price >= 0)
  ),
  CONSTRAINT fk_var_product FOREIGN KEY (product_id) REFERENCES products(product_id) ON DELETE CASCADE,
  CONSTRAINT fk_var_color   FOREIGN KEY (color_id)   REFERENCES colors(color_id),
  CONSTRAINT fk_var_size    FOREIGN KEY (size_id)    REFERENCES sizes(size_id)
) ENGINE=InnoDB;

-- ---------------------------------------------------------------------
-- STOCK: current quantity per variant per location
-- ---------------------------------------------------------------------
CREATE TABLE stock_levels (
  variant_id   INT UNSIGNED NOT NULL,
  location_id  INT UNSIGNED NOT NULL,
  quantity     INT NOT NULL DEFAULT 0,
  updated_at   TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (variant_id, location_id),
  CONSTRAINT chk_qty_nonneg CHECK (quantity >= 0),
  CONSTRAINT fk_sl_variant  FOREIGN KEY (variant_id)  REFERENCES product_variants(variant_id) ON DELETE CASCADE,
  CONSTRAINT fk_sl_location FOREIGN KEY (location_id) REFERENCES locations(location_id)
) ENGINE=InnoDB;

-- ---------------------------------------------------------------------
-- STOCK MOVEMENTS: append-only audit log. Never update or delete rows.
-- ---------------------------------------------------------------------
CREATE TABLE stock_movements (
  movement_id    BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
  variant_id     INT UNSIGNED NOT NULL,
  location_id    INT UNSIGNED NOT NULL,
  movement_type  ENUM('RECEIVE','SALE','RETURN','ADJUSTMENT','STOCKTAKE',
                      'TRANSFER_IN','TRANSFER_OUT','DAMAGED','LOST') NOT NULL,
  quantity_change INT NOT NULL,                  -- positive = added, negative = removed
  quantity_after  INT NOT NULL,                  -- running balance at time of movement
  reference      VARCHAR(100) NULL,              -- PO number, receipt number, etc.
  notes          VARCHAR(255) NULL,
  user_id        INT UNSIGNED NULL,
  source         ENUM('desktop','mobile','pos','import') NOT NULL DEFAULT 'desktop',
  created_at     TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
  INDEX idx_mov_variant_date (variant_id, created_at),
  INDEX idx_mov_type_date (movement_type, created_at),
  CONSTRAINT chk_mov_nonzero CHECK (quantity_change <> 0),
  CONSTRAINT fk_mov_variant  FOREIGN KEY (variant_id)  REFERENCES product_variants(variant_id),
  CONSTRAINT fk_mov_location FOREIGN KEY (location_id) REFERENCES locations(location_id),
  CONSTRAINT fk_mov_user     FOREIGN KEY (user_id)     REFERENCES users(user_id) ON DELETE SET NULL
) ENGINE=InnoDB;

-- ---------------------------------------------------------------------
-- PURCHASE ORDERS (optional but useful for receiving stock by scanning)
-- ---------------------------------------------------------------------
CREATE TABLE purchase_orders (
  po_id         INT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
  po_number     VARCHAR(30) NOT NULL UNIQUE,
  supplier_id   INT UNSIGNED NOT NULL,
  status        ENUM('draft','ordered','partial','received','cancelled') NOT NULL DEFAULT 'draft',
  ordered_at    DATE NULL,
  expected_at   DATE NULL,
  notes         VARCHAR(255) NULL,
  created_by    INT UNSIGNED NULL,
  created_at    TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
  CONSTRAINT fk_po_supplier FOREIGN KEY (supplier_id) REFERENCES suppliers(supplier_id),
  CONSTRAINT fk_po_user     FOREIGN KEY (created_by)  REFERENCES users(user_id) ON DELETE SET NULL
) ENGINE=InnoDB;

CREATE TABLE purchase_order_items (
  po_item_id    INT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
  po_id         INT UNSIGNED NOT NULL,
  variant_id    INT UNSIGNED NOT NULL,
  qty_ordered   INT UNSIGNED NOT NULL,
  qty_received  INT UNSIGNED NOT NULL DEFAULT 0,
  unit_cost     DECIMAL(10,2) NOT NULL,
  UNIQUE KEY uq_po_variant (po_id, variant_id),
  CONSTRAINT chk_po_qty CHECK (qty_ordered > 0),
  CONSTRAINT fk_poi_po      FOREIGN KEY (po_id)      REFERENCES purchase_orders(po_id) ON DELETE CASCADE,
  CONSTRAINT fk_poi_variant FOREIGN KEY (variant_id) REFERENCES product_variants(variant_id)
) ENGINE=InnoDB;

-- ---------------------------------------------------------------------
-- LABEL PRINT LOG (what barcode labels were printed, and when)
-- ---------------------------------------------------------------------
CREATE TABLE label_print_jobs (
  job_id       INT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
  variant_id   INT UNSIGNED NOT NULL,
  copies       SMALLINT UNSIGNED NOT NULL DEFAULT 1,
  printed_by   INT UNSIGNED NULL,
  printed_at   TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
  CONSTRAINT fk_lpj_variant FOREIGN KEY (variant_id) REFERENCES product_variants(variant_id),
  CONSTRAINT fk_lpj_user    FOREIGN KEY (printed_by) REFERENCES users(user_id) ON DELETE SET NULL
) ENGINE=InnoDB;

-- =====================================================================
-- VIEWS
-- =====================================================================

-- Full inventory listing: one row per variant per location, with effective prices
CREATE VIEW v_inventory AS
SELECT
  v.variant_id,
  p.product_id,
  p.style_code,
  p.name               AS product_name,
  b.name               AS brand,
  c.name               AS category,
  col.name             AS color,
  s.label              AS size,
  v.sku,
  v.barcode,
  l.location_id,
  l.name               AS location,
  COALESCE(sl.quantity, 0)                         AS quantity,
  COALESCE(v.cost_price,   p.base_cost)            AS cost_price,
  COALESCE(v.retail_price, p.base_price)           AS retail_price,
  COALESCE(sl.quantity, 0) * COALESCE(v.cost_price, p.base_cost) AS stock_value_cost,
  v.reorder_level,
  v.is_active
FROM product_variants v
JOIN products p        ON p.product_id = v.product_id
JOIN colors col        ON col.color_id = v.color_id
JOIN sizes s           ON s.size_id    = v.size_id
CROSS JOIN locations l
LEFT JOIN stock_levels sl ON sl.variant_id = v.variant_id AND sl.location_id = l.location_id
LEFT JOIN brands b     ON b.brand_id   = p.brand_id
LEFT JOIN categories c ON c.category_id = p.category_id
WHERE l.is_active = 1;

-- Items at or below their reorder level
CREATE VIEW v_low_stock AS
SELECT *
FROM v_inventory
WHERE is_active = 1 AND quantity <= reorder_level;

-- =====================================================================
-- STORED PROCEDURES
-- Both the desktop and mobile apps should call these instead of
-- updating stock_levels directly, so the log and balance never diverge.
-- =====================================================================

DELIMITER $$

-- Add or remove stock (positive = in, negative = out). Atomic.
CREATE PROCEDURE sp_adjust_stock(
  IN p_variant_id   INT UNSIGNED,
  IN p_location_id  INT UNSIGNED,
  IN p_change       INT,
  IN p_type         VARCHAR(20),
  IN p_user_id      INT UNSIGNED,
  IN p_source       VARCHAR(10),
  IN p_reference    VARCHAR(100),
  IN p_notes        VARCHAR(255)
)
BEGIN
  DECLARE v_new_qty INT;

  DECLARE EXIT HANDLER FOR SQLEXCEPTION
  BEGIN
    ROLLBACK;
    RESIGNAL;
  END;

  IF p_change = 0 THEN
    SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'Quantity change cannot be zero';
  END IF;

  START TRANSACTION;

  -- Make sure a row exists, then lock it
  INSERT INTO stock_levels (variant_id, location_id, quantity)
  VALUES (p_variant_id, p_location_id, 0)
  ON DUPLICATE KEY UPDATE quantity = quantity;

  SELECT quantity INTO v_new_qty
  FROM stock_levels
  WHERE variant_id = p_variant_id AND location_id = p_location_id
  FOR UPDATE;

  SET v_new_qty = v_new_qty + p_change;

  IF v_new_qty < 0 THEN
    SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'Insufficient stock for this operation';
  END IF;

  UPDATE stock_levels
  SET quantity = v_new_qty
  WHERE variant_id = p_variant_id AND location_id = p_location_id;

  INSERT INTO stock_movements
    (variant_id, location_id, movement_type, quantity_change, quantity_after,
     reference, notes, user_id, source)
  VALUES
    (p_variant_id, p_location_id, p_type, p_change, v_new_qty,
     p_reference, p_notes, p_user_id, p_source);

  COMMIT;
END$$

-- Set an absolute count (used for stocktake / physical counts)
CREATE PROCEDURE sp_set_stock_count(
  IN p_variant_id   INT UNSIGNED,
  IN p_location_id  INT UNSIGNED,
  IN p_counted_qty  INT,
  IN p_user_id      INT UNSIGNED,
  IN p_source       VARCHAR(10),
  IN p_notes        VARCHAR(255)
)
BEGIN
  DECLARE v_current INT DEFAULT 0;
  DECLARE v_diff INT;

  SELECT COALESCE(quantity, 0) INTO v_current
  FROM stock_levels
  WHERE variant_id = p_variant_id AND location_id = p_location_id;

  SET v_diff = p_counted_qty - v_current;

  IF v_diff <> 0 THEN
    CALL sp_adjust_stock(p_variant_id, p_location_id, v_diff, 'STOCKTAKE',
                         p_user_id, p_source, NULL, p_notes);
  END IF;
END$$

-- Look up a variant by scanned barcode (what the mobile app calls on scan)
CREATE PROCEDURE sp_lookup_barcode(IN p_barcode VARCHAR(48))
BEGIN
  SELECT * FROM v_inventory WHERE barcode = p_barcode;
END$$

DELIMITER ;

-- =====================================================================
-- SEED DATA (sample data so you can test immediately)
-- =====================================================================

INSERT INTO users (username, password_hash, full_name, role)
VALUES ('admin', 'REPLACE_WITH_REAL_HASH', 'Store Admin', 'admin');

INSERT INTO locations (name, type) VALUES ('Main Store', 'store');

INSERT INTO sizes (label, sort_order) VALUES
  ('XS',10),('S',20),('M',30),('L',40),('XL',50),('XXL',60),('One Size',100);

INSERT INTO colors (name, code, hex_value) VALUES
  ('Black','BLK','#000000'),
  ('White','WHT','#FFFFFF'),
  ('Navy','NVY','#001F3F'),
  ('Red','RED','#C0392B');

INSERT INTO brands (name) VALUES ('House Brand');

INSERT INTO categories (name) VALUES ('Tops');
INSERT INTO categories (parent_id, name)
  SELECT category_id, 'T-Shirts' FROM categories WHERE name = 'Tops' AND parent_id IS NULL;

INSERT INTO suppliers (name, contact_name, email)
VALUES ('Sample Apparel Co.', 'Jane Doe', 'orders@sampleapparel.example');

INSERT INTO products (style_code, name, brand_id, category_id, supplier_id, gender, base_cost, base_price)
SELECT 'CT-001', 'Classic Tee',
       (SELECT brand_id FROM brands WHERE name = 'House Brand'),
       (SELECT category_id FROM categories WHERE name = 'T-Shirts'),
       (SELECT supplier_id FROM suppliers LIMIT 1),
       'unisex', 6.50, 19.99;

-- Auto-generate variants: Classic Tee in Black/White/Navy x S/M/L/XL
-- SKU pattern: STYLE-COLOR-SIZE, barcode = SKU (Code 128 can encode this directly)
INSERT INTO product_variants (product_id, color_id, size_id, sku, barcode)
SELECT p.product_id, c.color_id, s.size_id,
       CONCAT(p.style_code, '-', c.code, '-', s.label),
       CONCAT(p.style_code, '-', c.code, '-', s.label)
FROM products p
JOIN colors c ON c.code IN ('BLK','WHT','NVY')
JOIN sizes  s ON s.label IN ('S','M','L','XL')
WHERE p.style_code = 'CT-001';

-- Receive some starting stock via the procedure (also creates audit rows)
CALL sp_adjust_stock(
  (SELECT variant_id FROM product_variants WHERE sku = 'CT-001-BLK-M'),
  1, 10, 'RECEIVE', 1, 'desktop', 'INITIAL', 'Opening stock');
CALL sp_adjust_stock(
  (SELECT variant_id FROM product_variants WHERE sku = 'CT-001-WHT-L'),
  1, 1, 'RECEIVE', 1, 'desktop', 'INITIAL', 'Opening stock');

-- Quick checks:
--   SELECT * FROM v_inventory WHERE quantity > 0;
--   SELECT * FROM v_low_stock;
--   CALL sp_lookup_barcode('CT-001-BLK-M');
--   CALL sp_adjust_stock(<variant_id>, 1, -1, 'SALE', 1, 'mobile', NULL, NULL);
