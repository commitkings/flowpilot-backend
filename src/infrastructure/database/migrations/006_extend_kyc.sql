-- Migration 006: Extend kyc_submission with business-type-specific fields
-- Supports: limited_company, ngo, partnership, sole_proprietorship, mda

ALTER TABLE kyc_submission
    ADD COLUMN IF NOT EXISTS business_type          VARCHAR(50),
    ADD COLUMN IF NOT EXISTS registration_number    VARCHAR(100),
    ADD COLUMN IF NOT EXISTS tin_number             VARCHAR(50),

    -- NGO / Non-profit
    ADD COLUMN IF NOT EXISTS trustee_name           VARCHAR(255),
    ADD COLUMN IF NOT EXISTS trustee_bvn            VARCHAR(20),
    ADD COLUMN IF NOT EXISTS trustee_id_key         VARCHAR(512),
    ADD COLUMN IF NOT EXISTS scuml_number           VARCHAR(100),
    ADD COLUMN IF NOT EXISTS scuml_letter_key       VARCHAR(512),

    -- Partnership
    ADD COLUMN IF NOT EXISTS partner_names          TEXT,
    ADD COLUMN IF NOT EXISTS partner_id_key         VARCHAR(512),

    -- Government / MDA
    ADD COLUMN IF NOT EXISTS mda_letter_key         VARCHAR(512),
    ADD COLUMN IF NOT EXISTS authorized_officer_name VARCHAR(255),
    ADD COLUMN IF NOT EXISTS authorized_officer_bvn  VARCHAR(20),
    ADD COLUMN IF NOT EXISTS authorized_officer_id_key VARCHAR(512);

COMMENT ON COLUMN kyc_submission.business_type IS
    'Entity type: limited_company | ngo | partnership | sole_proprietorship | mda';
COMMENT ON COLUMN kyc_submission.registration_number IS
    'CAC RC number or equivalent registration number for the entity';
COMMENT ON COLUMN kyc_submission.partner_names IS
    'JSON array of partner full names (Partnership type)';
