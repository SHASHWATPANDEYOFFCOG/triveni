-- The ONLY surface the question-answering layer may query.
--
-- Narrow on purpose. A text-to-SQL system pointed at a whole schema will
-- eventually join something it should not, aggregate across a boundary that
-- makes no sense, or read a column whose meaning it has guessed. Three views,
-- documented column by column, remove that entire class of failure: there is
-- nothing here to get wrong, because there is nothing here that is not already
-- reconciled.
--
-- Every number in these views came out of the pipeline. None is derived at
-- query time from anything the model said.

-- ---------------------------------------------------------------------------
-- v_settlements - one row per settlement, with its waterfall already balanced.
-- ---------------------------------------------------------------------------
CREATE OR REPLACE VIEW v_settlements AS
SELECT
    settlement_id,          -- gateway settlement id, e.g. setl_601010000489
    settled_on,             -- DATE the bank actually credited it
    gross_paise,            -- sum of captures attributed to this settlement
    net_expected_paise,     -- gross less every named deduction
    net_received_paise,     -- what the bank statement shows
    residual_paise,         -- net_expected - net_received; 0 when balanced
    balanced,               -- BOOLEAN: did the waterfall close to zero
    member_count,           -- how many payments were netted into it
    mdr_paise,              -- merchant discount rate withheld
    gst_paise,              -- 18% GST on the MDR, not on the sale
    tds_paise,              -- 0.1% TDS under section 194-O
    refunds_paise,          -- refunds netted into this same cycle
    chargeback_paise,       -- funds withheld against disputes
    reserve_paise           -- rolling reserve held back
FROM settlements_raw;

-- ---------------------------------------------------------------------------
-- v_exceptions - what needs a human, typed into the closed taxonomy.
-- ---------------------------------------------------------------------------
CREATE OR REPLACE VIEW v_exceptions AS
SELECT
    exception_id,
    exception_type,         -- one of the 16 ExceptionType values, never free text
    severity,               -- info | low | medium | high
    amount_paise,
    as_of,                  -- DATE the exception was raised
    reason,                 -- the human-readable sentence
    evidence_count,         -- how many pieces of support are attached
    stage                   -- which pipeline stage produced it
FROM exceptions_raw;

-- ---------------------------------------------------------------------------
-- v_daily_cash - reconciled cash landing per day. The forecast's input.
-- ---------------------------------------------------------------------------
CREATE OR REPLACE VIEW v_daily_cash AS
SELECT
    value_date,             -- DATE
    credited_paise,         -- total credited that day
    credit_count,           -- number of bank credits
    is_business_day         -- BOOLEAN, per the Indian bank calendar
FROM daily_cash_raw;
