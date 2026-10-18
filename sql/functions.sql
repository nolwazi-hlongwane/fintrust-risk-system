-- =============================================================================
-- FinTrust: stored function flag_account
-- Brief slot: Week 14 Day 1 (Write API), listed under Pillar 2 as the required
-- stored procedure or function.
--
-- Run after schema.sql and before db_users.sql:   psql -f sql/functions.sql
-- Safe to run more than once (CREATE OR REPLACE).
--
-- flag_account(account_id, reason, flagged_by)
--   Records WHY an account was flagged and WHO flagged it (a risk_events row)
--   and marks the account FLAGGED, as ONE atomic step. flagged_by is the
--   signed-in officer's username, which the API takes from their Cognito
--   sign-in (Week 15 Day 2), never from the request. A function always runs inside a single database
--   transaction, so either both changes happen or neither does: an account can
--   never be FLAGGED with no recorded reason, or have a reason with no flag.
--
--   Returns the id of the new risk_events row.
--
-- SECURITY DEFINER: the function runs with the rights of its owner (the admin
--   user), not of whoever calls it. The API's database user has NO right to
--   insert into risk_events or to update accounts directly; it may only EXECUTE
--   this function. So the only way the API can change an account's status is
--   this one audited path. search_path is fixed so the function cannot be
--   tricked into using a look-alike table from another schema.
-- =============================================================================

-- The two-argument version (Week 14 Day 1) recorded no name. It is removed so
-- that no path can flag an account without saying who did it.
DROP FUNCTION IF EXISTS flag_account(VARCHAR, VARCHAR);

CREATE OR REPLACE FUNCTION flag_account(p_account_id VARCHAR, p_reason VARCHAR, p_flagged_by VARCHAR)
RETURNS BIGINT
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public, pg_temp
AS $$
DECLARE
    v_status        accounts.status%TYPE;
    v_risk_event_id risk_events.risk_event_id%TYPE;
BEGIN
    IF p_reason IS NULL OR length(btrim(p_reason)) = 0 THEN
        RAISE EXCEPTION 'A reason is required to flag an account'
            USING ERRCODE = 'check_violation';
    END IF;

    IF p_flagged_by IS NULL OR length(btrim(p_flagged_by)) = 0 THEN
        RAISE EXCEPTION 'The name of the person flagging the account is required'
            USING ERRCODE = 'check_violation';
    END IF;

    -- FOR UPDATE locks the account row until this transaction ends, so two
    -- people flagging the same account at the same moment cannot interfere.
    SELECT status INTO v_status
    FROM accounts
    WHERE account_id = p_account_id
    FOR UPDATE;

    IF NOT FOUND THEN
        RAISE EXCEPTION 'Account % does not exist', p_account_id
            USING ERRCODE = 'no_data_found';
    END IF;

    IF v_status = 'CLOSED' THEN
        RAISE EXCEPTION 'Account % is closed and cannot be flagged', p_account_id
            USING ERRCODE = 'object_not_in_prerequisite_state';
    END IF;

    INSERT INTO risk_events (account_id, transaction_id, event_type, points, reason, flagged_by)
    VALUES (p_account_id, NULL, 'MANUAL_FLAG', 0, btrim(p_reason), btrim(p_flagged_by))
    RETURNING risk_event_id INTO v_risk_event_id;

    UPDATE accounts
    SET status = 'FLAGGED'
    WHERE account_id = p_account_id;

    RETURN v_risk_event_id;
END;
$$;

COMMENT ON FUNCTION flag_account(VARCHAR, VARCHAR, VARCHAR) IS
    'Atomically records a MANUAL_FLAG risk event, with who flagged it, and sets the account status to FLAGGED';

-- Nobody may run it unless explicitly granted (see db_users.sql).
REVOKE ALL ON FUNCTION flag_account(VARCHAR, VARCHAR, VARCHAR) FROM PUBLIC;
