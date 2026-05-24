# GoldTrader Pro v4 — PostgreSQL Schema & Backend Spec
# Taxly India Private Limited
# All tables are tenant-isolated via tenant_id
# Mobile number is PRIMARY KEY for customers (globally unique per tenant)

-- ═══════════════════════════════════════════════════════════════
-- DATABASE CREATION
-- ═══════════════════════════════════════════════════════════════

CREATE DATABASE goldtrader_pro;
\c goldtrader_pro;

CREATE EXTENSION IF NOT EXISTS "uuid-ossp";
CREATE EXTENSION IF NOT EXISTS "pgcrypto";

-- ═══════════════════════════════════════════════════════════════
-- TENANTS (Managed by Taxly Admin)
-- ═══════════════════════════════════════════════════════════════

CREATE TABLE tenants (
    id              SERIAL PRIMARY KEY,
    company_name    VARCHAR(200) NOT NULL,
    gstin           VARCHAR(15),
    phone           VARCHAR(15),
    email           VARCHAR(100),
    address         TEXT,
    state           VARCHAR(50),
    logo_url        TEXT,
    plan            VARCHAR(20) DEFAULT 'demo'
                    CHECK (plan IN ('demo','annual','expired')),
    demo_expires_at TIMESTAMPTZ,
    is_active       BOOLEAN DEFAULT TRUE,
    created_at      TIMESTAMPTZ DEFAULT NOW(),
    updated_at      TIMESTAMPTZ DEFAULT NOW()
);

-- ═══════════════════════════════════════════════════════════════
-- USERS
-- ═══════════════════════════════════════════════════════════════

CREATE TABLE users (
    id              SERIAL PRIMARY KEY,
    tenant_id       INTEGER REFERENCES tenants(id) ON DELETE CASCADE,
    username        VARCHAR(50) NOT NULL,
    mobile          VARCHAR(15) NOT NULL,          -- used as login identifier too
    email           VARCHAR(100),
    password_hash   TEXT NOT NULL,
    role            VARCHAR(20) DEFAULT 'user'
                    CHECK (role IN ('admin','user','viewer')),
    auth_provider   VARCHAR(20) DEFAULT 'password'
                    CHECK (auth_provider IN ('password','google')),
    google_id       VARCHAR(100),
    approval_status VARCHAR(20) DEFAULT 'approved'
                    CHECK (approval_status IN ('pending','approved','rejected','trial')),
    trial_expires_at TIMESTAMPTZ,                    -- Google 10-day trial expiry
    company_name    VARCHAR(200),                    -- For Google signups
    is_active       BOOLEAN DEFAULT TRUE,
    created_at      TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE(tenant_id, username),
    UNIQUE(tenant_id, mobile)
);

CREATE INDEX idx_users_tenant ON users(tenant_id);
CREATE INDEX idx_users_mobile ON users(mobile);

-- ═══════════════════════════════════════════════════════════════
-- CUSTOMERS  ← MOBILE IS PRIMARY KEY (per tenant)
-- ═══════════════════════════════════════════════════════════════

CREATE TABLE customers (
    mobile          VARCHAR(15) NOT NULL,          -- PRIMARY KEY per tenant
    tenant_id       INTEGER REFERENCES tenants(id) ON DELETE CASCADE,
    name            VARCHAR(200) NOT NULL,
    pan             VARCHAR(10),                   -- mandatory if cash_fy > 200000
    state           VARCHAR(50) NOT NULL,          -- GST state (mandatory)
    gstin           VARCHAR(15),
    address         TEXT,
    email           VARCHAR(100),
    cash_receipts_fy NUMERIC(15,2) DEFAULT 0,     -- rolling FY total cash
    sft_flagged     BOOLEAN DEFAULT FALSE,          -- cash > 2L in FY
    created_at      TIMESTAMPTZ DEFAULT NOW(),
    updated_at      TIMESTAMPTZ DEFAULT NOW(),
    PRIMARY KEY (tenant_id, mobile)               -- composite PK
);

CREATE INDEX idx_customers_tenant ON customers(tenant_id);
CREATE INDEX idx_customers_name ON customers(tenant_id, name);

-- ═══════════════════════════════════════════════════════════════
-- INVOICES
-- ═══════════════════════════════════════════════════════════════

CREATE TABLE invoices (
    id              SERIAL PRIMARY KEY,
    tenant_id       INTEGER REFERENCES tenants(id) ON DELETE CASCADE,
    invoice_no      VARCHAR(30) NOT NULL,           -- e.g. INV-1-0284
    invoice_date    DATE NOT NULL,
    customer_mobile VARCHAR(15) NOT NULL,           -- FK to customers.mobile
    customer_name   VARCHAR(200) NOT NULL,
    customer_pan    VARCHAR(10),
    customer_state  VARCHAR(50),
    customer_gstin  VARCHAR(15),
    pay_mode        VARCHAR(20) NOT NULL
                    CHECK (pay_mode IN ('Cash','UPI','Card','NEFT/RTGS','Cheque')),
    gst_type        VARCHAR(20) DEFAULT 'CGST+SGST'
                    CHECK (gst_type IN ('CGST+SGST','IGST','Exempt')),
    gst_rate        NUMERIC(5,2) DEFAULT 3.00,
    subtotal        NUMERIC(15,2) DEFAULT 0,
    cgst            NUMERIC(15,2) DEFAULT 0,
    sgst            NUMERIC(15,2) DEFAULT 0,
    igst            NUMERIC(15,2) DEFAULT 0,
    tcs_applicable  BOOLEAN DEFAULT FALSE,
    tcs_base        NUMERIC(15,2) DEFAULT 0,
    tcs_amount      NUMERIC(15,2) DEFAULT 0,       -- 1% of tcs_base
    round_off       NUMERIC(10,2) DEFAULT 0,       -- rounding adjustment (can be +ve or -ve)
    grand_total     NUMERIC(15,2) DEFAULT 0,
    amount_paid     NUMERIC(15,2) DEFAULT 0,
    outstanding     NUMERIC(15,2) DEFAULT 0,
    status          VARCHAR(20) DEFAULT 'active'
                    CHECK (status IN ('active','cancelled','draft')),
    payment_status  VARCHAR(20) DEFAULT 'unpaid'
                    CHECK (payment_status IN ('paid','partial','unpaid')),
    notes           TEXT,
    created_by      INTEGER REFERENCES users(id),
    created_at      TIMESTAMPTZ DEFAULT NOW(),
    updated_at      TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE(tenant_id, invoice_no),
    FOREIGN KEY (tenant_id, customer_mobile) REFERENCES customers(tenant_id, mobile)
);

CREATE INDEX idx_invoices_tenant ON invoices(tenant_id);
CREATE INDEX idx_invoices_date ON invoices(tenant_id, invoice_date);
CREATE INDEX idx_invoices_customer ON invoices(tenant_id, customer_mobile);

-- ═══════════════════════════════════════════════════════════════
-- INVOICE ITEMS
-- ═══════════════════════════════════════════════════════════════

CREATE TABLE invoice_items (
    id              SERIAL PRIMARY KEY,
    invoice_id      INTEGER REFERENCES invoices(id) ON DELETE CASCADE,
    tenant_id       INTEGER REFERENCES tenants(id) ON DELETE CASCADE,
    category        VARCHAR(30) NOT NULL
                    CHECK (category IN ('Gold','Silver','Diamond','Polish Charges')),
    purity          VARCHAR(10),                   -- 24K/22K/20K/18K/14K/std/—
    description     VARCHAR(300) NOT NULL,
    hsn_code        VARCHAR(10) DEFAULT '7113',    -- 7113 for jewellery (non-polish)
    qty             NUMERIC(12,3) NOT NULL,
    unit            VARCHAR(5) NOT NULL CHECK (unit IN ('grm','crt')),
    rate            NUMERIC(15,2) NOT NULL,
    polish_charges  NUMERIC(15,2) NOT NULL DEFAULT 0,  -- polish qty * rate (calculation only, no stock link)
    making_charges  NUMERIC(15,2) DEFAULT 0,
    amount          NUMERIC(15,2) NOT NULL,        -- (qty*rate) + (polish_charges*rate) + making_charges
    created_at      TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX idx_items_invoice ON invoice_items(invoice_id);
CREATE INDEX idx_items_tenant ON invoice_items(tenant_id);

-- ═══════════════════════════════════════════════════════════════
-- PAYMENTS
-- ═══════════════════════════════════════════════════════════════

CREATE TABLE payments (
    id              SERIAL PRIMARY KEY,
    tenant_id       INTEGER REFERENCES tenants(id) ON DELETE CASCADE,
    invoice_id      INTEGER REFERENCES invoices(id),
    customer_mobile VARCHAR(15) NOT NULL,
    amount          NUMERIC(15,2) NOT NULL,
    payment_date    DATE NOT NULL,
    pay_mode        VARCHAR(20) NOT NULL,
    reference_no    VARCHAR(100),
    notes           TEXT,
    created_by      INTEGER REFERENCES users(id),
    created_at      TIMESTAMPTZ DEFAULT NOW(),
    FOREIGN KEY (tenant_id, customer_mobile) REFERENCES customers(tenant_id, mobile)
);

CREATE INDEX idx_payments_tenant ON payments(tenant_id);
CREATE INDEX idx_payments_invoice ON payments(invoice_id);
CREATE INDEX idx_payments_customer ON payments(tenant_id, customer_mobile);

-- ═══════════════════════════════════════════════════════════════
-- CASH REGISTER
-- ═══════════════════════════════════════════════════════════════

CREATE TABLE cash_register (
    id              SERIAL PRIMARY KEY,
    tenant_id       INTEGER REFERENCES tenants(id) ON DELETE CASCADE,
    entry_date      DATE NOT NULL,
    entry_type      VARCHAR(20) NOT NULL
                    CHECK (entry_type IN ('cash_in','cash_out','cash_to_bank','bank_in')),
    amount          NUMERIC(15,2) NOT NULL,
    description     TEXT NOT NULL,
    invoice_id      INTEGER REFERENCES invoices(id),
    bank_reference  VARCHAR(100),
    running_balance NUMERIC(15,2),                -- cash balance after this entry
    created_by      INTEGER REFERENCES users(id),
    created_at      TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX idx_cash_tenant ON cash_register(tenant_id);
CREATE INDEX idx_cash_date ON cash_register(tenant_id, entry_date);

-- ═══════════════════════════════════════════════════════════════
-- ADVANCES
-- ═══════════════════════════════════════════════════════════════

CREATE TABLE advances (
    id              SERIAL PRIMARY KEY,
    tenant_id       INTEGER REFERENCES tenants(id) ON DELETE CASCADE,
    customer_mobile VARCHAR(15) NOT NULL,
    amount          NUMERIC(15,2) NOT NULL,
    remaining       NUMERIC(15,2) NOT NULL,        -- unallocated balance
    advance_date    DATE NOT NULL,
    pay_mode        VARCHAR(20) NOT NULL,
    notes           TEXT,
    created_by      INTEGER REFERENCES users(id),
    created_at      TIMESTAMPTZ DEFAULT NOW(),
    FOREIGN KEY (tenant_id, customer_mobile) REFERENCES customers(tenant_id, mobile)
);

CREATE TABLE advance_allocations (
    id              SERIAL PRIMARY KEY,
    tenant_id       INTEGER REFERENCES tenants(id),
    advance_id      INTEGER REFERENCES advances(id),
    invoice_id      INTEGER REFERENCES invoices(id),
    allocated_amount NUMERIC(15,2) NOT NULL,
    allocated_at    TIMESTAMPTZ DEFAULT NOW(),
    created_by      INTEGER REFERENCES users(id)
);

-- ═══════════════════════════════════════════════════════════════
-- STOCK MASTER
-- ═══════════════════════════════════════════════════════════════

CREATE TABLE stock_items (
    id              SERIAL PRIMARY KEY,
    tenant_id       INTEGER REFERENCES tenants(id) ON DELETE CASCADE,
    category        VARCHAR(30) NOT NULL
                    CHECK (category IN ('Gold','Silver','Diamond','Polish Charges')),
    purity          VARCHAR(10),                   -- Gold: 24K/22K/20K/18K/14K; others: std/null
    description     VARCHAR(300) NOT NULL,
    unit            VARCHAR(5) NOT NULL CHECK (unit IN ('grm','crt')),
    qty_on_hand     NUMERIC(15,3) DEFAULT 0,
    fifo_enabled    BOOLEAN DEFAULT TRUE,
    is_active       BOOLEAN DEFAULT TRUE,
    created_at      TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE stock_transactions (
    id              SERIAL PRIMARY KEY,
    tenant_id       INTEGER REFERENCES tenants(id) ON DELETE CASCADE,
    stock_item_id   INTEGER REFERENCES stock_items(id),
    txn_type        VARCHAR(20) NOT NULL
                    CHECK (txn_type IN ('purchase','sale','adjustment','opening')),
    qty             NUMERIC(15,3) NOT NULL,        -- positive=in, negative=out
    purchase_rate   NUMERIC(15,2),                 -- for FIFO lots (purchases only)
    invoice_id      INTEGER REFERENCES invoices(id),
    reason          TEXT,
    txn_date        DATE NOT NULL,
    lot_remaining   NUMERIC(15,3),                 -- for FIFO: qty remaining in this lot
    created_by      INTEGER REFERENCES users(id),
    created_at      TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX idx_stock_tenant ON stock_items(tenant_id);
CREATE INDEX idx_stock_txn_item ON stock_transactions(stock_item_id);
CREATE INDEX idx_stock_txn_date ON stock_transactions(tenant_id, txn_date);

-- ═══════════════════════════════════════════════════════════════
-- USEFUL VIEWS
-- ═══════════════════════════════════════════════════════════════

-- Outstanding per invoice
CREATE VIEW v_outstanding AS
SELECT
    i.tenant_id,
    i.invoice_no,
    i.invoice_date,
    i.customer_mobile,
    i.customer_name,
    i.grand_total,
    COALESCE(SUM(p.amount),0) + COALESCE(SUM(aa.allocated_amount),0) AS total_received,
    i.grand_total - (COALESCE(SUM(p.amount),0) + COALESCE(SUM(aa.allocated_amount),0)) AS outstanding
FROM invoices i
LEFT JOIN payments p ON p.invoice_id = i.id
LEFT JOIN advance_allocations aa ON aa.invoice_id = i.id
WHERE i.status = 'active'
GROUP BY i.id;

-- Cash position at date
CREATE VIEW v_cash_position AS
SELECT
    tenant_id,
    SUM(CASE WHEN entry_type IN ('cash_in') THEN amount ELSE 0 END) AS cash_collected_fy,
    SUM(CASE WHEN entry_type = 'cash_to_bank' THEN amount ELSE 0 END) AS cash_deposited_fy,
    SUM(CASE WHEN entry_type = 'cash_in' THEN amount
             WHEN entry_type IN ('cash_out','cash_to_bank') THEN -amount
             ELSE 0 END) AS cash_on_hand
FROM cash_register
WHERE DATE_PART('year', entry_date + INTERVAL '3 months') =
      DATE_PART('year', CURRENT_DATE + INTERVAL '3 months')  -- Indian FY
GROUP BY tenant_id;

-- SFT: customers with cash > 2L in FY
CREATE VIEW v_sft_customers AS
SELECT
    c.tenant_id,
    c.mobile,
    c.name,
    c.pan,
    SUM(p.amount) FILTER (WHERE i.pay_mode = 'Cash') AS cash_receipts_fy
FROM customers c
JOIN invoices i ON i.tenant_id = c.tenant_id AND i.customer_mobile = c.mobile
JOIN payments p ON p.invoice_id = i.id
WHERE DATE_PART('year', i.invoice_date + INTERVAL '3 months') =
      DATE_PART('year', CURRENT_DATE + INTERVAL '3 months')
GROUP BY c.tenant_id, c.mobile, c.name, c.pan
HAVING SUM(p.amount) FILTER (WHERE i.pay_mode = 'Cash') > 200000;

-- TCS register: cash invoices > 5L
CREATE VIEW v_tcs_register AS
SELECT
    i.tenant_id,
    i.invoice_no,
    i.invoice_date,
    i.customer_mobile,
    i.customer_name,
    i.customer_pan,
    i.grand_total,
    i.tcs_base,
    i.tcs_amount
FROM invoices i
WHERE i.tcs_applicable = TRUE
  AND i.status = 'active';

-- GSTR-1 register
CREATE VIEW v_gstr1 AS
SELECT
    i.tenant_id,
    i.invoice_no,
    i.invoice_date,
    i.customer_name,
    i.customer_mobile,
    i.customer_gstin,
    i.customer_state,
    it.hsn_code,
    i.gst_type,
    i.subtotal,
    i.cgst,
    i.sgst,
    i.igst,
    i.grand_total
FROM invoices i
JOIN invoice_items it ON it.invoice_id = i.id
WHERE i.status = 'active'
GROUP BY i.id, it.hsn_code;

-- ═══════════════════════════════════════════════════════════════
-- BUSINESS LOGIC TRIGGERS
-- ═══════════════════════════════════════════════════════════════

-- 1. Auto-calculate TCS when invoice is created with Cash > 5L
CREATE OR REPLACE FUNCTION calculate_tcs()
RETURNS TRIGGER AS $$
BEGIN
    IF NEW.pay_mode = 'Cash' AND NEW.subtotal > 500000 THEN
        NEW.tcs_applicable := TRUE;
        NEW.tcs_base := NEW.subtotal;
        NEW.tcs_amount := ROUND(NEW.subtotal * 0.01, 2);
        NEW.grand_total := NEW.subtotal + NEW.cgst + NEW.sgst + NEW.igst + NEW.tcs_amount;
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER trg_tcs_calc
BEFORE INSERT OR UPDATE ON invoices
FOR EACH ROW EXECUTE FUNCTION calculate_tcs();

-- 2. Update customer SFT flag when payment recorded
CREATE OR REPLACE FUNCTION update_customer_sft()
RETURNS TRIGGER AS $$
DECLARE
    v_cash_total NUMERIC;
    v_inv_pay_mode VARCHAR;
BEGIN
    SELECT pay_mode INTO v_inv_pay_mode FROM invoices WHERE id = NEW.invoice_id;
    IF v_inv_pay_mode = 'Cash' THEN
        SELECT COALESCE(SUM(p.amount), 0) INTO v_cash_total
        FROM payments p
        JOIN invoices i ON i.id = p.invoice_id
        WHERE i.tenant_id = NEW.tenant_id
          AND i.customer_mobile = NEW.customer_mobile
          AND i.pay_mode = 'Cash'
          AND DATE_PART('year', i.invoice_date + INTERVAL '3 months') =
              DATE_PART('year', CURRENT_DATE + INTERVAL '3 months');

        UPDATE customers
        SET cash_receipts_fy = v_cash_total,
            sft_flagged = (v_cash_total > 200000)
        WHERE tenant_id = NEW.tenant_id AND mobile = NEW.customer_mobile;
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER trg_sft_update
AFTER INSERT ON payments
FOR EACH ROW EXECUTE FUNCTION update_customer_sft();

-- 3. Auto-generate invoice number per tenant
CREATE OR REPLACE FUNCTION generate_invoice_no()
RETURNS TRIGGER AS $$
DECLARE
    v_count INTEGER;
BEGIN
    SELECT COUNT(*) + 1 INTO v_count FROM invoices WHERE tenant_id = NEW.tenant_id;
    NEW.invoice_no := 'INV-' || NEW.tenant_id || '-' || LPAD(v_count::TEXT, 4, '0');
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER trg_invoice_no
BEFORE INSERT ON invoices
FOR EACH ROW
WHEN (NEW.invoice_no IS NULL OR NEW.invoice_no = '')
EXECUTE FUNCTION generate_invoice_no();

-- ═══════════════════════════════════════════════════════════════
-- BACKEND API ENDPOINTS (FastAPI / Node.js)
-- ═══════════════════════════════════════════════════════════════

/*
AUTH
  POST /api/auth/login                 { username/mobile, password }
  POST /api/auth/google                { google_token }
  POST /api/auth/signup-demo           { name, mobile, company, password }
  POST /api/auth/logout

INVOICES
  GET  /api/invoices?from=&to=&mobile=&status=
  POST /api/invoices                   { ...invoice data }
  GET  /api/invoices/:id
  PUT  /api/invoices/:id/cancel
  GET  /api/invoices/:id/pdf           → returns PDF bytes

CUSTOMERS (keyed by mobile)
  GET  /api/customers?q=&mobile=
  POST /api/customers                  { mobile (PK), name, state, pan, ... }
  PUT  /api/customers/:mobile
  POST /api/customers/import-excel     multipart Excel upload
  GET  /api/customers/:mobile/ledger

CASH REGISTER
  GET  /api/cash?from=&to=&as_of=
  POST /api/cash
  GET  /api/cash/summary?as_of=        → { cash_collected_fy, cash_deposited_fy, cash_on_hand }

ADVANCES
  GET  /api/advances?mobile=
  POST /api/advances
  POST /api/advances/:id/allocate      { allocations: [{invoice_id, amount}] }

STOCK
  GET  /api/stock
  POST /api/stock
  POST /api/stock/:id/adjust           { qty_change, purchase_rate, reason }
  GET  /api/stock/fifo-report?as_of=   → FIFO valuation

REPORTS
  GET  /api/reports/sales?from=&to=
  GET  /api/reports/payments?from=&to=
  GET  /api/reports/outstanding?mobile=
  GET  /api/reports/tcs?fy=
  GET  /api/reports/sft?fy=
  GET  /api/reports/account?from=&to=&view=invoice|item
  GET  /api/reports/gstr1?from=&to=

EXPORT
  GET  /api/export/excel?type=full|sales|tcs|sft|gstr1|account|cash
       → Returns xlsx with formatted sheets + all reports

ADMIN (Taxly only)
  POST /api/admin/login                { username:'Taxly', password }
  GET  /api/admin/tenants
  POST /api/admin/tenants
  PUT  /api/admin/tenants/:id/toggle
  PUT  /api/admin/tenants/:id/reset-password
  GET  /api/admin/users
  GET  /api/admin/google-requests
  PUT  /api/admin/google-requests/:id/approve
  GET  /api/admin/backups/:tenant_id   → download tenant backup
*/

-- ═══════════════════════════════════════════════════════════════
-- TECH STACK RECOMMENDATION
-- ═══════════════════════════════════════════════════════════════

/*
Backend:  FastAPI (Python 3.11+) + SQLAlchemy + Alembic migrations
Database: PostgreSQL 15+ (Supabase or RDS)
Auth:     JWT tokens + Google OAuth2 (python-jose, google-auth)
PDF:      WeasyPrint or ReportLab
Excel:    openpyxl (export) + pandas (import)
Cache:    Redis (session, FY totals)
Storage:  S3 / Cloudflare R2 (logos, backups)
Frontend: Vanilla JS (as built) or React
Deploy:   Docker + nginx
SSL:      Let's Encrypt

ENVIRONMENT VARIABLES:
  DATABASE_URL=postgresql://user:pass@host:5432/goldtrader_pro
  JWT_SECRET=<32-char random>
  GOOGLE_CLIENT_ID=<from Google Console>
  ADMIN_PASSWORD_HASH=<bcrypt hash of @Gsf025@>
  S3_BUCKET=goldtrader-backups
  REDIS_URL=redis://localhost:6379/0
*/
