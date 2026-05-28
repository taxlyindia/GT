-- ============================================================
-- GoldTrader Pro v4 — SQL Views & Business Logic Triggers
-- File: 02_views_and_triggers.sql
-- Run AFTER 01_schema.sql
-- ============================================================

-- ════════════════════════════════════════════════════════════
-- USEFUL VIEWS
-- ════════════════════════════════════════════════════════════

-- Outstanding per invoice (real-time)
CREATE OR REPLACE VIEW v_outstanding AS
SELECT
    i.tenant_id,
    i.invoice_no,
    i.invoice_date,
    i.customer_mobile,
    i.customer_name,
    i.pay_mode,
    i.grand_total,
    COALESCE(SUM(p.amount), 0)
        + COALESCE(SUM(aa.allocated_amount), 0)        AS total_received,
    i.grand_total
        - COALESCE(SUM(p.amount), 0)
        - COALESCE(SUM(aa.allocated_amount), 0)        AS outstanding
FROM invoices i
LEFT JOIN payments p             ON p.invoice_id = i.id
LEFT JOIN advance_allocations aa ON aa.invoice_id = i.id
WHERE i.status = 'active'
GROUP BY i.id;


-- Cash position per tenant for current Indian FY
-- Indian FY starts 1-Apr, ends 31-Mar
CREATE OR REPLACE VIEW v_cash_fy AS
SELECT
    tenant_id,
    SUM(CASE WHEN entry_type = 'cash_in'
             AND (EXTRACT(MONTH FROM entry_date) >= 4
                  OR EXTRACT(YEAR FROM entry_date) > EXTRACT(YEAR FROM CURRENT_DATE) - 1)
             THEN amount ELSE 0 END)                   AS cash_collected_fy,
    SUM(CASE WHEN entry_type = 'cash_to_bank'
             THEN amount ELSE 0 END)                   AS cash_deposited_fy,
    SUM(CASE WHEN entry_type IN ('cash_in','bank_in')  THEN  amount
             WHEN entry_type IN ('cash_out','cash_to_bank') THEN -amount
             ELSE 0 END)                               AS cash_on_hand
FROM cash_register
WHERE
    -- Current Indian FY filter
    entry_date >= MAKE_DATE(
        CASE WHEN EXTRACT(MONTH FROM CURRENT_DATE) >= 4
             THEN EXTRACT(YEAR FROM CURRENT_DATE)::INT
             ELSE EXTRACT(YEAR FROM CURRENT_DATE)::INT - 1
        END, 4, 1)
GROUP BY tenant_id;


-- SFT: customers with cash receipts > ₹2,00,000 in FY
CREATE OR REPLACE VIEW v_sft_customers AS
SELECT
    c.tenant_id,
    c.mobile,
    c.name,
    c.pan,
    c.cash_receipts_fy,
    CASE WHEN c.pan IS NULL OR c.pan = '' THEN 'PAN Required' ELSE 'Flag for SFT' END AS sft_status
FROM customers c
WHERE c.sft_flagged = TRUE;


-- TCS register: only invoices with TCS applied
CREATE OR REPLACE VIEW v_tcs_register AS
SELECT
    i.tenant_id,
    i.invoice_no,
    i.invoice_date,
    i.customer_mobile,
    i.customer_name,
    COALESCE(i.customer_pan, 'MISSING')     AS customer_pan,
    i.grand_total                           AS invoice_value,
    i.tcs_base,
    i.tcs_amount,
    i.pay_mode
FROM invoices i
WHERE i.tcs_applicable = TRUE
  AND i.status = 'active';


-- GSTR-1 view with HSN
CREATE OR REPLACE VIEW v_gstr1 AS
SELECT
    i.tenant_id,
    i.invoice_no,
    i.invoice_date,
    i.customer_name,
    i.customer_mobile,
    COALESCE(i.customer_gstin, 'Unregistered') AS gstin,
    i.customer_state,
    '7113'               AS hsn_code,
    i.gst_type,
    i.subtotal           AS taxable_value,
    i.gst_rate / 2       AS cgst_rate,
    i.cgst,
    i.gst_rate / 2       AS sgst_rate,
    i.sgst,
    i.igst,
    i.grand_total
FROM invoices i
WHERE i.status = 'active';


-- Google trial status view
CREATE OR REPLACE VIEW v_google_trial_status AS
SELECT
    u.id,
    u.username                  AS name,
    u.email,
    u.company_name              AS company,
    u.mobile,
    u.approval_status           AS status,
    u.trial_expires_at,
    GREATEST(0, EXTRACT(DAY FROM (u.trial_expires_at - NOW()))::INT) AS days_left,
    u.created_at                AS signed_up
FROM users u
WHERE u.auth_provider = 'google'
ORDER BY u.created_at DESC;


-- ════════════════════════════════════════════════════════════
-- TRIGGERS
-- ════════════════════════════════════════════════════════════

-- 1. Auto-calculate TCS when invoice is saved
CREATE OR REPLACE FUNCTION fn_calc_tcs()
RETURNS TRIGGER AS $$
BEGIN
    -- Section 206C(1F): TCS 1% on Cash payments > ₹5,00,000
    IF NEW.pay_mode = 'Cash' AND NEW.subtotal > 500000 THEN
        NEW.tcs_applicable := TRUE;
        NEW.tcs_base       := NEW.subtotal;
        NEW.tcs_amount     := ROUND(NEW.subtotal * 0.01, 2);
        NEW.grand_total    := NEW.subtotal + NEW.cgst + NEW.sgst + NEW.igst + NEW.tcs_amount;
    ELSE
        NEW.tcs_applicable := FALSE;
        NEW.tcs_base       := 0;
        NEW.tcs_amount     := 0;
        NEW.grand_total    := NEW.subtotal + NEW.cgst + NEW.sgst + NEW.igst;
    END IF;

    -- Set outstanding = grand_total on initial insert
    IF TG_OP = 'INSERT' THEN
        NEW.outstanding := NEW.grand_total;
    END IF;

    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER trg_tcs_calc
BEFORE INSERT OR UPDATE OF subtotal, pay_mode, cgst, sgst, igst ON invoices
FOR EACH ROW EXECUTE FUNCTION fn_calc_tcs();


-- 2. Auto-generate invoice number
CREATE OR REPLACE FUNCTION fn_invoice_no()
RETURNS TRIGGER AS $$
DECLARE
    v_count INTEGER;
BEGIN
    IF NEW.invoice_no IS NULL OR NEW.invoice_no = '' THEN
        SELECT COUNT(*) + 1 INTO v_count
        FROM invoices
        WHERE tenant_id = NEW.tenant_id;
        NEW.invoice_no := 'INV-' || NEW.tenant_id::TEXT || '-' || LPAD(v_count::TEXT, 4, '0');
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER trg_invoice_no
BEFORE INSERT ON invoices
FOR EACH ROW EXECUTE FUNCTION fn_invoice_no();


-- 3. Update customer cash FY totals + SFT flag after payment
CREATE OR REPLACE FUNCTION fn_update_customer_sft()
RETURNS TRIGGER AS $$
DECLARE
    v_inv_mode TEXT;
    v_cash_total NUMERIC;
BEGIN
    -- Only update if this payment is against a Cash invoice
    SELECT pay_mode::TEXT INTO v_inv_mode
    FROM invoices
    WHERE id = NEW.invoice_id;

    IF v_inv_mode = 'Cash' THEN
        -- Recalculate total cash receipts for this customer in current FY
        SELECT COALESCE(SUM(p.amount), 0) INTO v_cash_total
        FROM payments p
        JOIN invoices i ON i.id = p.invoice_id
        WHERE p.tenant_id       = NEW.tenant_id
          AND p.customer_mobile = NEW.customer_mobile
          AND i.pay_mode        = 'Cash'
          AND i.invoice_date >= MAKE_DATE(
              CASE WHEN EXTRACT(MONTH FROM CURRENT_DATE) >= 4
                   THEN EXTRACT(YEAR FROM CURRENT_DATE)::INT
                   ELSE EXTRACT(YEAR FROM CURRENT_DATE)::INT - 1
              END, 4, 1);

        UPDATE customers
        SET cash_receipts_fy = v_cash_total,
            sft_flagged      = (v_cash_total > 200000),
            updated_at       = NOW()
        WHERE tenant_id = NEW.tenant_id
          AND mobile    = NEW.customer_mobile;
    END IF;

    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER trg_sft_update
AFTER INSERT ON payments
FOR EACH ROW EXECUTE FUNCTION fn_update_customer_sft();


-- 4. Auto-expire Google trials (run nightly via pg_cron or Celery)
-- This function is called by a scheduled job, not a trigger
CREATE OR REPLACE FUNCTION fn_expire_google_trials()
RETURNS INTEGER AS $$
DECLARE
    v_expired INTEGER;
BEGIN
    UPDATE users
    SET approval_status = 'pending'
    WHERE auth_provider    = 'google'
      AND approval_status  = 'trial'
      AND trial_expires_at < NOW();

    GET DIAGNOSTICS v_expired = ROW_COUNT;
    RETURN v_expired;
END;
$$ LANGUAGE plpgsql;

-- Schedule with pg_cron (requires pg_cron extension):
-- SELECT cron.schedule('expire-trials', '0 2 * * *', 'SELECT fn_expire_google_trials()');


-- 5. Update invoice payment_status when outstanding changes
CREATE OR REPLACE FUNCTION fn_update_payment_status()
RETURNS TRIGGER AS $$
BEGIN
    IF NEW.outstanding <= 0 THEN
        NEW.payment_status := 'paid';
    ELSIF NEW.amount_paid > 0 THEN
        NEW.payment_status := 'partial';
    ELSE
        NEW.payment_status := 'unpaid';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER trg_payment_status
BEFORE UPDATE OF outstanding, amount_paid ON invoices
FOR EACH ROW EXECUTE FUNCTION fn_update_payment_status();


-- ════════════════════════════════════════════════════════════
-- USEFUL QUERIES (for reference / reporting)
-- ════════════════════════════════════════════════════════════

-- Dashboard KPIs for tenant_id = 1
/*
SELECT
    COUNT(*)                                                    AS total_invoices,
    SUM(grand_total)                                            AS total_revenue,
    SUM(CASE WHEN payment_status != 'paid' THEN outstanding END) AS total_outstanding,
    (SELECT SUM(amount) FROM cash_register WHERE tenant_id=1
     AND entry_type='cash_in' AND entry_date >= '2025-04-01')   AS cash_collected_fy
FROM invoices
WHERE tenant_id = 1 AND status = 'active';
*/

-- Customers requiring PAN (SFT flagged)
/*
SELECT mobile, name, pan, cash_receipts_fy
FROM customers
WHERE tenant_id = 1 AND sft_flagged = TRUE
ORDER BY cash_receipts_fy DESC;
*/

-- TCS summary for 26Q filing
/*
SELECT
    COUNT(*)           AS invoices_with_tcs,
    SUM(tcs_base)      AS total_taxable_value,
    SUM(tcs_amount)    AS total_tcs_collected
FROM v_tcs_register
WHERE tenant_id = 1;
*/
