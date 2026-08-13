-- ============================================================
--  POSentine — schema v7: المسحوبات (withdrawals)
--  شغّله في: Supabase → SQL Editor → New query → Run
--  آمن للتشغيل أكتر من مرة (idempotent)
-- ============================================================
--  ليه الملف ده موجود (2026-08-13 — withdrawals closure task):
--
--  المصدر المؤكد لـ"مسحوبات" يومية الخزينة = SUM(dbo.Personal.peramount)
--  جوه نافذة الوردية. المؤكد: المصدر = dbo.Personal (تحقيق العميل)،
--  والمسحوبات بتتخصم من الإجمالي (تأكيد المالك).
--
--  الإجمالي = مبيعات + مقبوضات − مرتجع − دليفري − مسحوبات
--
--  جدول withdrawals بيتكتب من الأجينت (نفس policy الـagent_rw بتاعة بقية
--  جداول الاستيعاب)، وبيتقرا من الـorchestrator/service_role. الحذف بيعكس
--  حذف صفوف Personal من جهاز الـPOS (الجدول بيتقرا كله كل دورة).
--
--  ⚠️ ممنوع تعديل schema.sql. ده ملف إضافي مستقل — نفس نمط v2..v6.
-- ============================================================

create table if not exists withdrawals (
  tenant_id   uuid not null,
  source_id   uuid not null,
  perid       bigint not null,      -- 🔑 Personal.Perid
  perdate     timestamp not null,   -- وقت الـPOS المحلي — ممنوع تحويله
  peramount   numeric(12,2) not null default 0,
  peruser     bigint,               -- metadata/أدلة — التجميع بالنافذة مش بالمستخدم
  perbr_id    bigint,               -- Personal.PerBRID (فرع/محل)
  pertype     integer,              -- Personal.pertype (metadata — الأدلة)
  pernote     text,                 -- Personal.pernote (وصف المسحوب)
  primary key (tenant_id, source_id, perid)
);

comment on table withdrawals is
  'مسحوبات يومية الخزينة = SUM(Personal.peramount) جوه نافذة الوردية — بتتخصم من الإجمالي';

create index if not exists ix_withdrawals_time
  on withdrawals (tenant_id, source_id, perdate);

-- RLS — نفس عزل بقية جداول الاستيعاب (العميل يشوف نفسه بس)
alter table withdrawals enable row level security;

drop policy if exists agent_rw on public.withdrawals;
create policy agent_rw on public.withdrawals
  for all
  using      ((auth.jwt() ->> 'tenant_id')::uuid = tenant_id)
  with check ((auth.jwt() ->> 'tenant_id')::uuid = tenant_id);

-- ============================================================
--  GRANT — أساسي وليس تجميلي (تم تطبيقه مباشرة في Supabase بعد
--  التحقق المباشر؛ راجع POST_MIGRATION_V7_VERIFICATION_REPORT.md)
-- ============================================================
--  Postgres بيفحص صلاحيات الجدول قبل RLS. من غير الـGRANT ده كان
--  كل طلب من الأجينت بيرجع 403 / 42501 "permission denied for table"
--  حتى مع policy صحيحة — الـpolicy مش بتتقيم أصلاً. نفس الدرس بتاع
--  schema_v2_grants.sql بالظبط، لجداول جديدة.
--
--  DELETE مقصود: الجدول مرآة حرفية لـ dbo.Personal (بيتقرا كله كل
--  دورة)، والـagent بيعكس أي حذف على الـPOS للسحاب.
grant select, insert, update, delete on public.withdrawals to authenticated;

-- ============================================================
--  shift_reports: عمود المسحوبات + تحديث قيد الإجمالي
-- ============================================================
--  القيد القديم (v6): grand_total = sales + collections - returns - delivery
--  كان مكتمل وقتها (مفيش مسحوبات في الموديل). دلوقتي المعادلة فيها مسحوبات
--  فالقيد لازم يتحدث عشان الصفوف الجديدة (بمسحوبات غير صفرية) متترفضش.
--  بنشيل القيد القديم ونضيف الجديد بنفس الاسم — التراجع موثق في الآخر.
--  آمن على البيانات الموجودة: الصفوف القدام كلها withdrawals=0 (default)
--  فـ 0 = 0 - 0 يفضل صحيح.

alter table shift_reports
  add column if not exists withdrawals numeric(12,2) not null default 0;

do $$
begin
  if exists (select 1 from pg_constraint
             where conname = 'shift_reports_grand_total_formula') then
    alter table shift_reports
      drop constraint shift_reports_grand_total_formula;
  end if;
end $$;

alter table shift_reports
  add constraint shift_reports_grand_total_formula
  check (grand_total = sales + collections - returns - delivery - withdrawals);

comment on column shift_reports.withdrawals is
  'مسحوبات = SUM(Personal.peramount) جوه نافذة الوردية — تُخصم من الإجمالي';
comment on column shift_reports.grand_total is
  'يومية الخزينة: sales + collections − returns − delivery − withdrawals  (متحقق: 19,205)';

-- ============================================================
--  التحقق اليدوي (شغّل مرة واحدة في SQL Editor، مش اختبار دائم):
-- ============================================================
--  -- 1) القيد الجديد يقبل صف بمسحوبات (صحيحة الحساب)
--  begin;
--  insert into shift_reports (tenant_id, source_id, shift_date, shift_name,
--         window_start, window_end, sales, returns, delivery, collections,
--         withdrawals, grand_total)
--  select tenant_id, source_id, current_date, 'morning',
--         now(), now(), 100, 0, 0, 0, 20, 80     -- 100 - 20 = 80 ✓
--    from tenants limit 1
--  on conflict do nothing;
--  rollback;   -- لا نثبّت شيئاً
--
--  -- 2) القيد الجديد يرفض صف مخالف (code 23514)
--  begin;
--  insert into shift_reports (tenant_id, source_id, shift_date, shift_name,
--         window_start, window_end, sales, returns, delivery, collections,
--         withdrawals, grand_total)
--  select tenant_id, source_id, current_date, 'morning',
--         now(), now(), 100, 0, 0, 0, 20, 100    -- 100 != 80 → لازم يرفض
--    from tenants limit 1;
--  rollback;
--  -- المتوقع: ERROR: check constraint "shift_reports_grand_total_formula"
--
--  -- 3) الأجينت رفع withdrawals؟ (افترض أن المزامنة شغالة)
--  select tenant_id, source_id, count(*) from withdrawals group by 1, 2;
--
--  التراجع (rollback):
--   alter table shift_reports drop column withdrawals;
--   alter table shift_reports drop constraint shift_reports_grand_total_formula;
--   alter table shift_reports add constraint shift_reports_grand_total_formula
--     check (grand_total = sales + collections - returns - delivery);
--   drop table withdrawals;
--  (تراجع آمن: مفيش بيانات مهمة — الجدول يتعاد ملءه من الـPOS في دورة واحدة.)
-- ============================================================
