-- ============================================================
--  POSentine — schema v6: shift_reports.grand_total CHECK
--  شغّله في: Supabase → SQL Editor → New query → Run
--  آمن للتشغيل أكتر من مرة (idempotent)
-- ============================================================
--  ليه الملف ده موجود (H5 of the hardening phase, 2026-08-11):
--
--  The daybook formula lives only in Python (metrics.py compute_shift:
--  grand_total = sales + collections - returns - delivery). Nothing at
--  the database layer enforces it, so a future code path, a manual
--  UPDATE, or a bug could insert an internally-inconsistent row and
--  Postgres would accept it silently — the same shape as the original
--  1,140 EGP confusion, except now enforced structurally instead of
--  relying on every future code path getting the Python formula right.
--
--  Safe against existing production data (verified 2026-08-11): the
--  three already-sent false-green rows are all-zero and 0 = 0 + 0 - 0 - 0
--  is trivially true — the constraint cannot fail on them. It only
--  rejects a hand-written/buggy INSERT/UPDATE that violates the formula.
--  NULLs pass vacuously (NULL = ... is not FALSE), which matches the
--  existing code: every row the orchestrator writes sets all five fields.
--
--  ⚠️ ممنوع تعديل schema.sql. ده ملف إضافي مستقل.
-- ============================================================

do $$
begin
  if not exists (
    select 1 from pg_constraint where conname = 'shift_reports_grand_total_formula'
  ) then
    alter table shift_reports
      add constraint shift_reports_grand_total_formula
      check (grand_total = sales + collections - returns - delivery);
  end if;
end $$;

-- ============================================================
--  التحقق اليدوي (شغّل مرة واحدة في SQL Editor، مش اختبار دائم):
-- ============================================================
--  -- 1) الصف السليم يعدّي
--  begin;
--  insert into shift_reports (tenant_id, source_id, shift_date, shift_name,
--         window_start, window_end, sales, returns, delivery, collections,
--         grand_total)
--  select tenant_id, source_id, current_date, 'morning',
--         now(), now(), 100, 0, 0, 0, 100
--    from tenants limit 1
--  on conflict do nothing;
--  rollback;   -- لا نثبّت شيئاً
--
--  -- 2) الصف المخالف يُرفض بالكود 23514 (check_violation)
--  begin;
--  insert into shift_reports (tenant_id, source_id, shift_date, shift_name,
--         window_start, window_end, sales, returns, delivery, collections,
--         grand_total)
--  select tenant_id, source_id, current_date, 'morning',
--         now(), now(), 100, 0, 0, 0, 500   -- 500 != 100 → لازم يرفض
--    from tenants limit 1;
--  rollback;
--  -- المتوقع: ERROR: new row for relation "shift_reports" violates
--  -- check constraint "shift_reports_grand_total_formula"
--  -- SQLSTATE 23514
--
--  -- 3) ثم تأكد أن تشغيل delivery حقيقي بعده لسه بيشتغل (الصيغة
--  --    متحققة بالبناء — معملوش أي تغيير على السلوك).
--
--  التراجع (rollback):
--   alter table shift_reports drop constraint shift_reports_grand_total_formula;
--  (تراجع آمن: مفيش أي تغيير بيانات — القيد نفسه بس.)
