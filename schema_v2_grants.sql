-- ============================================================
--  POSentine — schema v2: table privileges
--  شغّله في: Supabase → SQL Editor → New query → Run
--  آمن للتشغيل أكتر من مرة (idempotent)
-- ============================================================
--  ليه الملف ده موجود:
--
--  schema.sql فعّل RLS على 15 جدول وكتب policies لـ7 منهم، بس
--  عمره ما عمل GRANT. في PostgreSQL فحص الصلاحيات بيتم **قبل** RLS،
--  يعني الـpolicies دي عمرها ما اتقريت أصلاً.
--
--  المُتحقَّق منه (2026-08-09، الـ15 جدول، الدورين):
--      GET /rest/v1/<any table>  →  403/401  code 42501
--      hint: GRANT SELECT ON public.<table> TO service_role
--
--  من غير الملف ده النظام كله مقفول: الأوركستريتور مش قادر يقرا
--  tenants، والأجينت مش قادر يكتب invoices.
--
--  ⚠️ ممنوع تعديل schema.sql. ده ملف إضافي مستقل.
-- ============================================================

-- ── 1) الوصول للـschema ─────────────────────────────────────
grant usage on schema public to anon, authenticated, service_role;

-- ── 2) service_role — الأوركستريتور (GitHub Actions فقط) ────
--     بيتخطى RLS أصلاً، ومحتاج كل الجداول: قراءة الإعدادات،
--     كتابة events و outbox و shift_reports و internal_anomalies.
grant all privileges on all tables    in schema public to service_role;
grant all privileges on all sequences in schema public to service_role;

-- ── 3) authenticated — الأجينت على جهاز العميل ──────────────
--     🔒 أقل صلاحية ممكنة: السبع جداول اللي ليها policy بس،
--     من غير delete، ومن غير أي وصول لـevents/outbox/tenants.
--     العزل بين العملاء بيفضل شغّال بالـRLS فوق الصلاحيات دي.
grant select, insert, update on
    public.sync_state,
    public.heartbeats,
    public.pos_users,
    public.pos_products,
    public.invoices,
    public.invoice_lines,
    public.cash_counts
  to authenticated;

--     heartbeats.id هو bigserial — محتاج الـsequence عشان الـinsert يشتغل
grant usage, select on sequence public.heartbeats_id_seq to authenticated;

-- ── 4) anon — لا شيء ────────────────────────────────────────
--     الـanon key بيتبعت في هيدر apikey للدخول من البوابة بس.
--     الدور الفعلي بييجي من الـJWT في Authorization، فـanon
--     مش محتاج ولا صلاحية على أي جدول. سيبها كده.

-- ── 5) الجداول اللي هتتعمل بعدين ────────────────────────────
alter default privileges in schema public
  grant all privileges on tables to service_role;
alter default privileges in schema public
  grant all privileges on sequences to service_role;

-- ============================================================
--  التحقق بعد التشغيل — لازم يرجّع صفوف للدورين
-- ============================================================
-- select grantee, table_name, string_agg(privilege_type, ',' order by privilege_type)
--   from information_schema.role_table_grants
--  where table_schema = 'public'
--    and grantee in ('service_role','authenticated')
--  group by grantee, table_name
--  order by grantee, table_name;
