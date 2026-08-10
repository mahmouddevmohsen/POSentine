-- ============================================================
--  POSentine — schema v4: recipients.notify_before_golive
--  شغّله في: Supabase → SQL Editor → New query → Run
--  آمن للتشغيل أكتر من مرة (idempotent)
-- ============================================================
--  ليه الملف ده موجود:
--
--  Task 1's schema audit (reports/phase2/TASK_01_SCHEMA_REPORT.md)
--  found this column ABSENT from schema.sql and flagged it as
--  blocking Task 3's go-live bypass until approved and applied.
--  It was never committed as a migration file.
--
--  During the Phase 2 final audit (2026-08-11) a live GitHub Actions
--  dry-run + live run against production Supabase proved the column
--  DOES exist and DOES work — the dev-chat bypass correctly gated a
--  recipient in before go_live_at was set. Someone applied the
--  migration directly in the Supabase SQL Editor, outside git.
--
--  This file does not apply anything new — it documents, for the
--  audit trail and for schema.sql's own "committed schema is the
--  contract" rule, what is already live. schema.sql itself is a
--  locked Phase-1 file and is not touched.
-- ============================================================

alter table recipients
  add column if not exists notify_before_golive boolean not null default false;

comment on column recipients.notify_before_golive is
  'مستقبِل تجريبي: يستقبل حتى قبل ضبط tenants.go_live_at. للمطوّر فقط.';

-- ============================================================
--  التحقق — العمود موجود ونوعه صحيح
-- ============================================================
-- select column_name, data_type, is_nullable, column_default
--   from information_schema.columns
--  where table_schema = 'public' and table_name = 'recipients'
--    and column_name = 'notify_before_golive';
