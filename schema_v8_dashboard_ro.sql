-- ============================================================
--  POSentine — schema v8: dashboard read-only role
--  شغّله في: Supabase → SQL Editor → New query → Run
--  آمن للتشغيل أكتر من مرة (idempotent)
-- ============================================================
--  ليه الملف ده موجود (2026-08-16 — Phase 3: secure dashboard integration):
--
--  الداشبورد (المالك) محتاج يقرا جداول التقارير، بس مفيش أي صلاحية
--  موجودة للدور اللي ممكن المتصفح يستخدمه:
--      anon            = لا شيء (متعمد — schema_v2 §4)
--      authenticated   = 7 جداول استيعاب بس + withdrawals، وكتابة فيها
--                        (ممنوع إعادة استخدامه — أي credential من الدور ده
--                        قادر يكتب، مش read-only)
--      service_role    = كل حاجة بس server-only (GitHub Actions) وممنوع
--                        يوصل للمتصفح
--  وده متأكد منه مباشرة (2026-08-16، live probe):
--      GET /rest/v1/shift_reports      → 403/42501
--      GET /rest/v1/events             → 403/42501
--      GET /rest/v1/tenants            → 403/42501
--      GET /rest/v1/internal_anomalies → 403/42501
--
--  الحل — نفس نمط الأجينت المثبت (mint_agent_token + RLS + tenant claim):
--      دور جديد اسمه dashboard_ro، صلاحيات SELECT فقط، وpolicy رصاصة
--      tenant-scoped. المتصفح يمسك JWT بدور dashboard_ro و claim التيننت
--      (بيتصنع بـ mint_dashboard_token.py) — زي ما الأجينت شغال بالظبط،
--      بس قراءة بس ومفيش كتابة في أي حتة.
--
--  ⚠️ ممنوع تعديل schema.sql. ده ملف إضافي مستقل — نفس نمط v2..v7.
-- ============================================================

-- ── 1) الدور — قراءة فقط، من غير تسجيل دخول مباشر ────────────
do $$
begin
  if not exists (select 1 from pg_roles where rolname = 'dashboard_ro') then
    create role dashboard_ro nologin;
  end if;
end $$;

comment on role dashboard_ro is
  'دور الداشبورد (المالك) — SELECT فقط على جداول التقارير، معزول بالتيننت عبر RLS. لا يكتب أي حاجة.';

-- 🔴 إلزامي — من غيره الملف مش هيشتغل (اكتُشف بالمراجعة قبل التفعيل، 2026-08-16):
-- PostgREST بيتصل بدور authenticator وبيعمل SET LOCAL ROLE <دور الـJWT> لكل طلب.
-- لو الدور الجديد مش عضو في authenticator، الطلب يفشل عند تبديل الدور نفس نفسه
-- (403/42501) حتى لو الـGRANT والـRLS صح — docs.postgrest.org/auth:
--   "the database administrator must allow the authenticator role to switch
--    into this user by previously executing GRANT user123 TO authenticator".
-- نفس النمط الموثق في Supabase Custom Roles (storage): "Important to grant the
-- role to the authenticator" — grant manager to authenticator;
grant dashboard_ro to authenticator;

-- ── 2) الوصول للـschema والجداول (SELECT فقط — لا insert/update/delete) ──
grant usage on schema public to dashboard_ro;

grant select on
    public.tenants,
    public.shift_reports,
    public.events,
    public.internal_anomalies,
    public.withdrawals,
    public.heartbeats,
    public.cash_counts,
    public.pos_users,
    public.pos_products,
    public.sync_state
  to dashboard_ro;

-- ملاحظة: invoice/invoice_lines مش متضمنين عمداً — الداشبورد مايعيدش حساب
-- فواتير خام (ممنوع: بيضاعف الحساب المعتمد وبيعمل single-source-of-truth
-- مكسور). بيقرا shift_reports المحسوبة من الأوركستريتور بس.

-- ── 3) RLS — عزل التيننت للدور الجديد (نفس claim بتاع الأجينت) ──
--     الجداول دي ليها RLS مفعل من schema.sql من غير policies — من غير
--     الـpolicy دي أي قراءة بترجع صفر صفوف حتى مع الـGRANT.
--     ملاحظة: withdrawals/heartbeats/cash_counts/pos_users/pos_products/
--     sync_state عندهم agent_rw policy (for all) بتغطي SELECT بنفس الـclaim،
--     بس هنضيف policies صريحة لـ dashboard_ro عشان القراءة تبقى مستقلة
--     عن سياسات الكتابة بتاعة الأجينت ومفهومة في المراجعة.
--
--     🔴 تصحيح معماري (اكتُشف بخطأ تشغيل حقيقي 42703): جدول tenants معرف
--     هويته بعمود `id` (PK) — مفيش عمود اسمه tenant_id جواه. الجداول
--     التانية كلها عندها tenant_id. فـ policy بتاعة tenants بتقارن بـ id،
--     و policy بتاعة الجداول التانية بـ tenant_id.

do $$
declare t text;
begin
  foreach t in array array[
    'shift_reports','events','internal_anomalies',
    'withdrawals','heartbeats','cash_counts','pos_users','pos_products','sync_state'
  ] loop
    execute format($f$
      drop policy if exists dashboard_ro_select on public.%I;
      create policy dashboard_ro_select on public.%I
        for select
        to dashboard_ro
        using ((auth.jwt() ->> 'tenant_id')::uuid = tenant_id);
    $f$, t, t);
  end loop;
end $$;

-- tenants: الهوية = عمود id (PK) مش tenant_id — عمود ده مش موجود أصلًا
-- في الجدول ده (مصدر الخطأ 42703 وقت التفعيل). نفس الـclaim، مقارنة بـ id.
drop policy if exists dashboard_ro_select on public.tenants;
create policy dashboard_ro_select on public.tenants
  for select
  to dashboard_ro
  using ((auth.jwt() ->> 'tenant_id')::uuid = id);

-- ── 4) تأكيد نهائي: الدور مايقدرش يكتب في أي جدول ─────────────
--     (الفحص ده بيشتغل بعد الـGRANTs — لازم يرجّع صفر صفوف)
-- select grantee, privilege_type
--   from information_schema.role_table_grants
--  where grantee = 'dashboard_ro'
--    and privilege_type in ('INSERT','UPDATE','DELETE','TRUNCATE');

-- ============================================================
--  الخطوة التالية (خارج الملف ده — بالـJWT secret):
--  python mint_dashboard_token.py --tenant-id <uuid>
--  → token بدور dashboard_ro + tenant claim — يحط في إعداد الداشبورد.
-- ============================================================
