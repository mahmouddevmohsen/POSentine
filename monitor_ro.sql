-- ============================================================
-- monitor_ro.sql — the POS login's permissions, as a file
-- ============================================================
-- Run against the HD Soft database on the customer's SQL Server, as an
-- administrator. Idempotent: re-running it re-asserts the same state and
-- changes nothing else.
--
-- WHY THIS FILE EXISTS
--
-- Until now `monitor_ro`'s permissions existed only as something someone
-- typed into a management tool once. The whole read-only guarantee rested
-- on a login nobody could reproduce, inspect in review, or re-assert after
-- a change. That is an audit finding, not a preference.
--
-- WHAT IT DOES BEYOND db_datareader + db_denydatawriter
--
-- `db_denydatawriter` denies exactly INSERT, UPDATE and DELETE. Everything
-- else that can change the database — DDL, TRUNCATE, BACKUP, EXECUTE — is
-- blocked today only because nobody granted it. An absence can be handed
-- out by a helpful administrator without anyone touching the DENY.
--
-- This file converts those absences into explicit DENYs. A DENY overrides
-- any GRANT, so a later mistake cannot quietly re-open one.
--
-- The most important line is `DENY EXECUTE ON SCHEMA::dbo`. Under SQL
-- Server's ownership chaining, a stored procedure that shares an owner
-- with the tables it writes runs WITHOUT a permission check on those
-- tables — so `db_denydatawriter` would not stop it. EXECUTE is the one
-- path that defeats the DENY, and this closes it.
--
-- NOT IN THIS FILE: the password. It is never committed, never echoed,
-- and never written to disk. Create the login separately; this script only
-- asserts what the login may do.
-- ============================================================

SET NOCOUNT ON;
GO

-- ------------------------------------------------------------
-- 0. Refuse to run against the wrong database
-- ------------------------------------------------------------
-- Applying a DENY set to master or msdb by mistake is its own incident.
IF DB_NAME() IN (N'master', N'model', N'msdb', N'tempdb')
BEGIN
    RAISERROR(N'Run this against the HD Soft database, not %s.', 16, 1, DB_NAME());
    SET NOEXEC ON;
END
GO

IF NOT EXISTS (SELECT 1 FROM sys.tables WHERE name = N'Sales')
BEGIN
    RAISERROR(N'No dbo.Sales in %s — this does not look like the HD Soft database.',
              16, 1, DB_NAME());
    SET NOEXEC ON;
END
GO

-- ------------------------------------------------------------
-- 1. The database user
-- ------------------------------------------------------------
-- The server login is created separately, with a password that never
-- appears in a file:
--
--   CREATE LOGIN [monitor_ro] WITH PASSWORD = N'<typed, not stored>',
--        CHECK_POLICY = ON, DEFAULT_DATABASE = [<the HD Soft database>];
--
IF NOT EXISTS (SELECT 1 FROM sys.database_principals WHERE name = N'monitor_ro')
BEGIN
    CREATE USER [monitor_ro] FOR LOGIN [monitor_ro];
END
GO

-- ------------------------------------------------------------
-- 2. Read, and refuse to write
-- ------------------------------------------------------------
ALTER ROLE [db_datareader]      ADD MEMBER [monitor_ro];
ALTER ROLE [db_denydatawriter]  ADD MEMBER [monitor_ro];
GO

-- Belt and braces: the role memberships above are what SQL Server checks,
-- and these are what an auditor reads. Both say the same thing on purpose.
DENY INSERT, UPDATE, DELETE ON SCHEMA::dbo TO [monitor_ro];
GO

-- ------------------------------------------------------------
-- 3. Close what db_denydatawriter does NOT cover
-- ------------------------------------------------------------

-- ALTER on a table is what TRUNCATE TABLE requires. Denying it here is the
-- difference between "nobody granted TRUNCATE" and "TRUNCATE is refused".
DENY ALTER, CONTROL, TAKE OWNERSHIP, REFERENCES ON SCHEMA::dbo TO [monitor_ro];
GO

-- The ownership-chaining hole. See the header.
DENY EXECUTE ON SCHEMA::dbo TO [monitor_ro];
GO

-- DDL at database scope: CREATE TABLE also governs SELECT ... INTO.
DENY CREATE TABLE, CREATE VIEW, CREATE PROCEDURE, CREATE FUNCTION,
     CREATE SCHEMA, CREATE SYNONYM, CREATE TYPE, CREATE RULE,
     CREATE DEFAULT, CREATE ASSEMBLY, CREATE AGGREGATE
  TO [monitor_ro];
GO

DENY ALTER ANY SCHEMA, ALTER ANY USER, ALTER ANY ROLE, ALTER ANY DATASPACE,
     ALTER ANY ASSEMBLY, ALTER ANY CERTIFICATE
  TO [monitor_ro];
GO

-- Not a write to the data, but it copies the customer's whole database.
DENY BACKUP DATABASE, BACKUP LOG TO [monitor_ro];
GO

-- ------------------------------------------------------------
-- 4. Verify — the same questions the on-site probe asks
-- ------------------------------------------------------------
-- Every value in the first result set must be 0. `readonly_probe.py` asks
-- the server these same questions on every install; this is here so a DBA
-- can confirm the state without running our tooling.

EXECUTE AS USER = N'monitor_ro';

SELECT
    HAS_PERMS_BY_NAME(N'dbo.Sales',  N'OBJECT', N'INSERT')  AS can_insert_sales,
    HAS_PERMS_BY_NAME(N'dbo.Sales',  N'OBJECT', N'UPDATE')  AS can_update_sales,
    HAS_PERMS_BY_NAME(N'dbo.Sales',  N'OBJECT', N'DELETE')  AS can_delete_sales,
    HAS_PERMS_BY_NAME(N'dbo.Sales',  N'OBJECT', N'ALTER')   AS can_alter_sales,   -- governs TRUNCATE
    HAS_PERMS_BY_NAME(N'dbo.Sales',  N'OBJECT', N'CONTROL') AS can_control_sales,
    HAS_PERMS_BY_NAME(DB_NAME(),     N'DATABASE', N'CREATE TABLE')    AS can_create_table,
    HAS_PERMS_BY_NAME(DB_NAME(),     N'DATABASE', N'ALTER')           AS can_alter_db,
    HAS_PERMS_BY_NAME(DB_NAME(),     N'DATABASE', N'BACKUP DATABASE') AS can_backup,
    IS_ROLEMEMBER(N'db_owner')      AS is_db_owner,
    IS_ROLEMEMBER(N'db_ddladmin')   AS is_ddladmin,
    IS_ROLEMEMBER(N'db_datawriter') AS is_datawriter,
    IS_SRVROLEMEMBER(N'sysadmin')   AS is_sysadmin;

-- And this must return exactly SELECT (plus CONNECT at database scope).
SELECT permission_name
  FROM fn_my_permissions(N'dbo.Sales', N'OBJECT')
 ORDER BY permission_name;

REVERT;
GO

SET NOEXEC OFF;
GO
