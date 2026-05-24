-- ============================================================
-- GoldTrader Pro — Supplier Module Migration
-- Run this SQL if your app uses Alembic migrations instead of
-- Base.metadata.create_all() (auto-create on startup).
-- If your main.py has create_all() in lifespan, this runs automatically
-- and you do NOT need to run this manually.
-- ============================================================

-- 1. Suppliers table (mirrors customers table pattern)
CREATE TABLE IF NOT EXISTS suppliers (
    mobile       VARCHAR(15)  NOT NULL,
    tenant_id    INTEGER      NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    name         VARCHAR(200) NOT NULL,
    gstin        VARCHAR(15),
    pan          VARCHAR(10),
    address      TEXT,
    email        VARCHAR(100),
    state        VARCHAR(50)  NOT NULL DEFAULT '',
    created_at   TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    updated_at   TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    PRIMARY KEY (mobile, tenant_id)
);
CREATE INDEX IF NOT EXISTS ix_suppliers_tenant ON suppliers(tenant_id);
CREATE INDEX IF NOT EXISTS ix_suppliers_name   ON suppliers(tenant_id, name);

-- 2. Supplier Invoices (purchase invoices from suppliers)
CREATE TABLE IF NOT EXISTS supplier_invoices (
    id               SERIAL PRIMARY KEY,
    tenant_id        INTEGER      NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    supplier_mobile  VARCHAR(15)  NOT NULL,
    supplier_name    VARCHAR(200) NOT NULL,
    invoice_no       VARCHAR(30)  NOT NULL,
    invoice_date     DATE         NOT NULL,
    gst_type         VARCHAR(20)  NOT NULL DEFAULT 'CGST+SGST',
    gst_rate         NUMERIC(5,2) NOT NULL DEFAULT 3,
    subtotal         NUMERIC(15,2) NOT NULL DEFAULT 0,
    cgst             NUMERIC(15,2) NOT NULL DEFAULT 0,
    sgst             NUMERIC(15,2) NOT NULL DEFAULT 0,
    igst             NUMERIC(15,2) NOT NULL DEFAULT 0,
    grand_total      NUMERIC(15,2) NOT NULL DEFAULT 0,
    amount_paid      NUMERIC(15,2) NOT NULL DEFAULT 0,
    outstanding      NUMERIC(15,2) NOT NULL DEFAULT 0,
    status           VARCHAR(20)  NOT NULL DEFAULT 'active',
    payment_status   VARCHAR(20)  NOT NULL DEFAULT 'unpaid',
    notes            TEXT,
    created_by       INTEGER REFERENCES users(id),
    created_at       TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    updated_at       TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    UNIQUE (tenant_id, invoice_no)
);
CREATE INDEX IF NOT EXISTS ix_sup_inv_tenant   ON supplier_invoices(tenant_id);
CREATE INDEX IF NOT EXISTS ix_sup_inv_supplier ON supplier_invoices(tenant_id, supplier_mobile);

-- 3. Supplier Invoice Items (line items → auto-updates stock on insert)
CREATE TABLE IF NOT EXISTS supplier_invoice_items (
    id             SERIAL PRIMARY KEY,
    invoice_id     INTEGER      NOT NULL REFERENCES supplier_invoices(id) ON DELETE CASCADE,
    tenant_id      INTEGER      NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    category       VARCHAR(30)  NOT NULL,
    purity         VARCHAR(10),
    description    VARCHAR(300) NOT NULL,
    hsn_code       VARCHAR(10)  NOT NULL DEFAULT '7113',
    qty            NUMERIC(12,3) NOT NULL,
    unit           VARCHAR(10)  NOT NULL,
    rate           NUMERIC(15,2) NOT NULL,
    making_charges NUMERIC(15,2) NOT NULL DEFAULT 0,
    amount         NUMERIC(15,2) NOT NULL,
    created_at     TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS ix_sup_item_invoice ON supplier_invoice_items(invoice_id);

-- 4. Supplier Payments
CREATE TABLE IF NOT EXISTS supplier_payments (
    id               SERIAL PRIMARY KEY,
    tenant_id        INTEGER      NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    supplier_mobile  VARCHAR(15)  NOT NULL,
    invoice_id       INTEGER REFERENCES supplier_invoices(id),
    amount           NUMERIC(15,2) NOT NULL,
    payment_date     DATE         NOT NULL,
    pay_mode         VARCHAR(20)  NOT NULL DEFAULT 'Cash',
    reference_no     VARCHAR(100),
    notes            TEXT,
    created_by       INTEGER REFERENCES users(id),
    created_at       TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS ix_sup_pay_tenant   ON supplier_payments(tenant_id);
CREATE INDEX IF NOT EXISTS ix_sup_pay_supplier ON supplier_payments(tenant_id, supplier_mobile);

-- 5. Supplier Advances
CREATE TABLE IF NOT EXISTS supplier_advances (
    id               SERIAL PRIMARY KEY,
    tenant_id        INTEGER      NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    supplier_mobile  VARCHAR(15)  NOT NULL,
    amount           NUMERIC(15,2) NOT NULL,
    remaining        NUMERIC(15,2) NOT NULL,
    advance_date     DATE         NOT NULL,
    pay_mode         VARCHAR(20)  NOT NULL DEFAULT 'Cash',
    notes            TEXT,
    created_by       INTEGER REFERENCES users(id),
    created_at       TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS ix_sup_adv_tenant ON supplier_advances(tenant_id);

-- Verify tables created
SELECT table_name FROM information_schema.tables
WHERE table_schema = 'public'
AND table_name IN ('suppliers','supplier_invoices','supplier_invoice_items','supplier_payments','supplier_advances')
ORDER BY table_name;
