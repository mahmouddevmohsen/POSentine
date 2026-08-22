-- ============================================================
--  POSentine — schema v12: persist authoritative withdrawal_users on shift_reports
--  شغّله في: Supabase → SQL Editor → New query → Run
--  آمن للتشغيل أكتر من مرة (idempotent)
-- ============================================================
--  ليه الملف ده موجود (2026-08-22 — Withdrawal user attribution):
--
--  orchestrator._sum_withdrawals() بيرجّع مجموع مسحوبات النافذة (float)
--  بس — القيمة دي هي المصدر المعتمد لسطر "مسحوبات" و grand_total، وهي
--  اللي فضلت زي ما هي. لكن كل صف مسحوب (dbo.Personal عبر withdrawals)
--  أصلاً بيحمل peruser — المستخدم اللي سجّله. القيمة دي كانت بتتحسب
--  (orchestrator._group_withdrawals_by_user، عرض بس) وبعدين تتشال قبل ما
--  تتسجل في shift_reports — نفس فخ top_items/other_users بالظبط.
--
--  الحل — نفس نمط schema_v9/v10 بالظبط: عمود إضافي، بيتملى وقت إنشاء
--  التقرير من نفس withdrawal_users اللي أصلاً بيتحسب، مفيش حساب جديد،
--  مفيش قراءة تانية لجدول withdrawals من المتصفح، مفيش تكرار حساب.
--
--  ⚠️ القيمة دي **بيانات عرض فقط** — مجموعها لازم يساوي شهادة
--  shift_reports.withdrawals (نفس الصفوف، نفس النافذة) لكنها مش المصدر
--  المالي المعتمد وممنوع تتغذى في grand_total أو أي حساب مالي تاني.
--
--  ⚠️ ممنوع تعديل schema.sql. ده ملف إضافي مستقل — نفس نمط v2..v11.
--  ⚠️ ممنوع تعديل withdrawals / invoices / invoice_lines أو أي قيد مالي
--  موجود — العمود ده إضافي بحت على shift_reports.
-- ============================================================

-- ── 1) العمود — JSONB، اختياري، بدون قيمة افتراضية غير NULL ──────
alter table public.shift_reports
  add column if not exists withdrawal_users jsonb;

comment on column public.shift_reports.withdrawal_users is
  'ناتج orchestrator._group_withdrawals_by_user() زي ما هو — مصفوفة كائنات '
  '[ {"uid":.., "name":.., "count":.., "amount":..}, ... ]، مرتبة amount '
  'تنازلي. بيانات عرض فقط لمين سجّل كل مسحوب (peruser) — مجموع amount هنا '
  'على نفس صفوف الوردية بيساوي shift_reports.withdrawals، لكن العمود ده '
  'مش المصدر المالي المعتمد وممنوع يتغذى في grand_total. مصفوفة فاضية [] '
  'لو مفيش مسحوبات في الوردية دي؛ NULL بس للورديات القديمة اللي اتسجلت '
  'قبل هذا العمود. مفيش حساب تاني بيحصل هنا.';

-- ── 2) الصلاحية — مضمونة أصلاً بالـGRANT الحالي (schema_v8)، بس ──
--     بنعيدها هنا صراحة عشان الملف يبقى مستقل وقابل للتشغيل لوحده من
--     غير الاعتماد على ترتيب تشغيل الملفات القديمة (نفس نمط v9/v10 —
--     dashboard_ro أصلاً عنده select على shift_reports بالكامل، فالعمود
--     الجديد بيتغطى تلقائي بمجرد إضافته).
grant select on public.shift_reports to dashboard_ro;

-- ============================================================
--  الخطوة التالية (خارج الملف ده):
--  orchestrator.py اتحدّث بالفعل عشان يكتب withdrawal_users جوه
--  out.shift_row وقت إنشاء كل تقرير وردية جديد (نفس التحديث اللي حصل
--  لـtop_items في schema_v9 ولـother_users في schema_v10). الورديات
--  القديمة تفضل withdrawal_users = NULL — سلوك متوقع وصريح، مش خطأ
--  (الداشبورد بيعرض حالة "لا توجد بيانات" مش رقم مختلق، ولا مستخدم مختلق).
--
--  التنفيذ: الملف ده **متوصفش**، مش متنفذ. لازم يتشغل يدوي في
--  Supabase → SQL Editor من صاحب المشروع، زي كل ملفات schema_v*.sql
--  اللي فاتت.
-- ============================================================
