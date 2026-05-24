-- ============================================================
-- GoldTrader Pro v4 — Sample Seed Data
-- File: 03_seed_data.sql
-- For development/testing only — DO NOT run in production
-- ============================================================

-- ── Tenant ───────────────────────────────────────────────────
INSERT INTO tenants (company_name, gstin, phone, email, state, plan, is_active)
VALUES ('Raj Jewellers Pvt Ltd', '27AAAPF0939F1ZV', '9876543210', 'raj@example.com', 'Maharashtra', 'annual', TRUE);

-- ── Admin User (password: admin123) ──────────────────────────
-- Hash generated with: bcrypt.hashpw(b'admin123', bcrypt.gensalt())
INSERT INTO users (tenant_id, username, mobile, password_hash, role, auth_provider, approval_status, is_active)
VALUES (1, 'rajesh_admin', '9876543210',
        '$2b$12$LQv3c1yqBWVHxkd0LHAkCOYz6TtxMebFIBDBdBBnNa.DV9aGQ5pQy',
        'admin', 'password', 'approved', TRUE);

INSERT INTO users (tenant_id, username, mobile, password_hash, role, auth_provider, approval_status, is_active)
VALUES (1, 'staff_rahul', '9800112233',
        '$2b$12$LQv3c1yqBWVHxkd0LHAkCOYz6TtxMebFIBDBdBBnNa.DV9aGQ5pQy',
        'user', 'password', 'approved', TRUE);

-- ── Google Trial User ─────────────────────────────────────────
INSERT INTO users (tenant_id, username, mobile, email, password_hash, role, auth_provider,
                   google_id, approval_status, trial_expires_at, company_name, is_active)
VALUES (2, 'Anita Mehta', '9876501234', 'anita.jewels@gmail.com',
        '$2b$12$placeholder',
        'admin', 'google', 'google_id_anita_001',
        'trial', NOW() + INTERVAL '7 days',
        'Anita Gold Works', TRUE);

-- ── Customers (mobile = primary key) ─────────────────────────
INSERT INTO customers (mobile, tenant_id, name, pan, state, gstin, address, cash_receipts_fy, sft_flagged)
VALUES
    ('9876543210', 1, 'Priya Sharma',  'ABCDE1234F', 'Maharashtra', '29AAAPF0939F1ZV', '123 MG Road, Mumbai',    45000,   FALSE),
    ('9812345678', 1, 'Arun Kumar',    'PQRST5678K', 'Delhi',        '',                '456 Park St, Delhi',    550000,  TRUE),
    ('9911223344', 1, 'Meena Devi',    NULL,          'Telangana',    '',                'Hyderabad',             285000,  TRUE),
    ('9934501234', 1, 'Suresh Iyer',   'LMNOP9012J', 'Maharashtra',  '27BBBPF1234G2ZX','Pune',                  18000,   FALSE),
    ('9900112233', 1, 'Deepak Sharma', 'PQRST5678K', 'Gujarat',      '',                'Ahmedabad',             700000,  TRUE);

-- ── Stock Items ───────────────────────────────────────────────
INSERT INTO stock_items (tenant_id, category, purity, description, unit, qty_on_hand, fifo_enabled)
VALUES
    (1, 'Gold',           '22K', 'Necklace weight',  'grm', 450.500, TRUE),
    (1, 'Gold',           '18K', 'Rings & earrings',  'grm', 320.000, TRUE),
    (1, 'Silver',         'std', '925 Silverware',    'grm', 12400.000, TRUE),
    (1, 'Diamond',        'std', 'VVS1 Solitaires',   'crt', 24.500, TRUE),
    (1, 'Polish Charges', NULL,  'Polish charges',    'grm', 0.000, FALSE);

-- ── Sample Invoice ────────────────────────────────────────────
INSERT INTO invoices (tenant_id, invoice_no, invoice_date, customer_mobile, customer_name,
                      customer_pan, customer_state, pay_mode, gst_type, gst_rate,
                      subtotal, cgst, sgst, tcs_applicable, tcs_base, tcs_amount,
                      grand_total, outstanding, status, payment_status, created_by)
VALUES
    (1, 'INV-1-0284', '2026-02-20', '9876543210', 'Priya Sharma',
     'ABCDE1234F', 'Maharashtra', 'UPI', 'CGST+SGST', 3,
     118000, 1770, 1770, FALSE, 0, 0,
     121540, 0, 'active', 'paid', 1),
    (1, 'INV-1-0283', '2026-02-19', '9812345678', 'Arun Kumar',
     'PQRST5678K', 'Delhi', 'Cash', 'CGST+SGST', 3,
     509226, 7639, 7639, TRUE, 509226, 5092,
     529596, 479596, 'active', 'partial', 1);

-- ── Invoice Items ─────────────────────────────────────────────
INSERT INTO invoice_items (invoice_id, tenant_id, category, purity, description, hsn_code, qty, unit, rate, making_charges, amount)
VALUES
    (1, 1, 'Gold', '22K', 'Gold Necklace Set 22K', '7113', 17.200, 'grm', 6800, 1400, 118800),
    (2, 1, 'Gold', '22K', 'Bridal Set 22K',        '7113', 74.300, 'grm', 6800, 3000, 512240);

-- ── Payment ───────────────────────────────────────────────────
INSERT INTO payments (tenant_id, invoice_id, customer_mobile, amount, payment_date, pay_mode, notes)
VALUES
    (1, 1, '9876543210', 121540, '2026-02-20', 'UPI',  'Full payment'),
    (1, 2, '9812345678', 50000,  '2026-02-19', 'Cash', 'Part payment');

-- ── Cash Register ─────────────────────────────────────────────
INSERT INTO cash_register (tenant_id, entry_date, entry_type, amount, description, invoice_id, running_balance)
VALUES
    (1, '2026-02-20', 'cash_in',      50000, 'Payment — Priya Sharma (INV-1-0284)', 1, 78450),
    (1, '2026-02-20', 'cash_to_bank', 50000, 'Deposit to HDFC Bank', NULL, 28450),
    (1, '2026-02-19', 'cash_in',      50000, 'Payment — Arun Kumar (INV-1-0283)',   2, 28450),
    (1, '2026-02-18', 'cash_out',     8000,  'Shop rent', NULL, 8450);
