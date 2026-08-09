-- ============================================================
--  POSentine — schema v3: strip inherited destructive privileges
--  شغّله في: Supabase → SQL Editor → New query → Run
--  آمن للتشغيل أكتر من مرة (idempotent)
-- ============================================================
--  ليه الملف ده موجود:
--
--  الـdefault ACL بتاع دور postgres في schema public بيدّي
--  anon و authenticated الحروف دي على **كل** جدول جديد:
--
--      anon          = Dxtm   →  TRUNCATE, REFERENCES, TRIGGER, MAINTAIN
--      authenticated = Dxtm
--
--  يعني كل جدول اتعمل ورّث TRUNCATE لـanon، وكل جدول هيتعمل
--  هيورّثها كمان. ودي مش حاجة RLS بيحميها — الـpolicies مالهاش
--  أي علاقة بـTRUNCATE.
--
--  المُتحقَّق منه (2026-08-09):
--      invoices relacl = {postgres=arwdDxtm/postgres,
--                         anon=Dxtm/postgres,
--                         authenticated=arwDxtm/postgres,
--                         service_role=arwdDxtm/postgres}
--
--  الأجينت على جهاز العميل بيشتغل بدور authenticated. المفروض
--  يكتب في 7 جداول وخلاص — مش إنه يقدر يفضّي tenants أو outbox.
--
--  ⚠️ ممنوع تعديل schema.sql. ده ملف إضافي مستقل.
-- ============================================================

-- ⚠️ الملف كله transaction واحدة. الخطوة 1 بتشيل صلاحيات الأجينت
--    والخطوة 3 بترجّعها — لازم يحصلوا مع بعض أو مايحصلوش خالص.
begin;

-- ── 1) شيل كل حاجة من anon و authenticated ──────────────────
--     بنشيل الكل وبعدين نرجّع المطلوب بالظبط، عشان منسيبش
--     حرف واحد ورا من غير ما ناخد بالنا.
revoke all privileges on all tables    in schema public from anon, authenticated;
revoke all privileges on all sequences in schema public from anon, authenticated;

-- ── 2) امنع التوريث ده في أي جدول جديد ──────────────────────
--     الخطوة 1 بتصلّح النهاردة بس. من غير الخطوة دي، أول جدول
--     جديد هيرث Dxtm تاني من نفس الـdefault ACL، ونرجع نعتمد
--     على إننا فاكرين. الاستبعاد يبقى بنيوي مش عادة.
--
--     'for role postgres' صريحة عن قصد: الـdefault ACL اللي
--     بيورّث Dxtm متسجّل باسم postgres تحديداً.
alter default privileges for role postgres in schema public
  revoke all privileges on tables    from anon, authenticated;
alter default privileges for role postgres in schema public
  revoke all privileges on sequences from anon, authenticated;

-- ── 3) رجّع اللي الأجينت محتاجه بالظبط — ولا حرف زيادة ──────
grant select, insert, update on
    public.sync_state,
    public.heartbeats,
    public.pos_users,
    public.pos_products,
    public.invoices,
    public.invoice_lines,
    public.cash_counts
  to authenticated;

grant usage, select on sequence public.heartbeats_id_seq to authenticated;

-- ── 4) anon: ولا حاجة. خالص. ────────────────────────────────
--     الـanon key بيتبعت في هيدر apikey للدخول من البوابة بس،
--     والدور الفعلي بييجي من الـJWT في Authorization.

-- ── 5) service_role زي ما هو (من v2) ────────────────────────
--     بيتخطى RLS أصلاً وبيشتغل في GitHub Actions بس.
grant usage on schema public to anon, authenticated, service_role;
grant all privileges on all tables    in schema public to service_role;
grant all privileges on all sequences in schema public to service_role;

commit;

-- ============================================================
--  ⚠️ حالة قايمة مش متقفلة: supabase_admin
--
--  الـdefault ACL بتاع supabase_admin بيدّي anon و authenticated
--  الحروف كلها (arwdDxtm) على أي جدول **هو** يعمله. جداولنا
--  اتعملت بـpostgres فالمسار ده مش شغّال دلوقتي، ومقدرناش
--  نغيّره — Management API مش بيشتغل بالدور ده.
--
--  عشان كده الفحص بتاع الصلاحيات اتحط في الـmonthly keepalive
--  workflow وبيفشل لو anon ظهرت على أي جدول في public.
--  الحاجة اللي مش بتتقفل، بتتخلّى تبان لوحدها.
-- ============================================================

-- ============================================================
--  التحقق — لازم anon ترجع صفر صفوف
-- ============================================================
-- select grantee, table_name,
--        string_agg(privilege_type, ',' order by privilege_type) as privs
--   from information_schema.role_table_grants
--  where table_schema = 'public'
--    and grantee in ('anon','authenticated','service_role')
--  group by grantee, table_name
--  order by grantee, table_name;
