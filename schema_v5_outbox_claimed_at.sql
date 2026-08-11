-- ============================================================
--  POSentine — schema v5: outbox.claimed_at
--  شغّله في: Supabase → SQL Editor → New query → Run
--  آمن للتشغيل أكتر من مرة (idempotent)
-- ============================================================
--  ليه الملف ده موجود (H4 of the hardening phase, 2026-08-11):
--
--  A row stuck at outbox.status='sending' after a process crash is
--  invisible forever: nothing records WHEN it entered that state, so
--  nothing can tell "claimed 200ms ago, still sending" from "claimed
--  3 days ago, orphaned by a crash". outbox has created_at and
--  sent_at but no claimed_at.
--
--  This additive column timestamps the existing 'sending' state (no
--  new state — purely observability). The notifier's _claim() PATCH
--  sets claimed_at alongside status='sending'; a scan then surfaces
--  rows stuck longer than the threshold as internal_anomalies kind
--  'stuck_sending' — a LOUD internal signal for a human to check the
--  Telegram chat history and decide. It explicitly does NOT
--  auto-resend or auto-mark-sent: Telegram's Bot API has no
--  idempotency key and no post-hoc "was message X delivered" query
--  for a private chat, so guessing would risk exactly the duplicate
--  owner-message outcome this project has already decided against.
--
--  ⚠️ MIGRATION ORDERING (hard requirement): apply THIS file and
--  verify the column live BEFORE the notifier code that writes it
--  deploys — a PATCH referencing a column PostgREST does not know is
--  rejected with HTTP 400. The notifier's feature-probe makes the
--  code safe either way (it omits claimed_at until the column
--  exists), but the intended sequence is: apply → verify → deploy.
--
--  ⚠️ ممنوع تعديل schema.sql. ده ملف إضافي مستقل.
-- ============================================================

alter table outbox
  add column if not exists claimed_at timestamptz;

comment on column outbox.claimed_at is
  'لحظة انتقال الصف إلى status=sending (المطالبة قبل الإرسال). '
  'لرصد الصفوف العالقة بعد crash — مش ولا حالة جديدة.';

-- ============================================================
--  التحقق — العمود موجود (يرجع صف واحد بقيمة فارغة أو null)
-- ============================================================
-- select id, status, claimed_at from outbox order by id limit 1;
--
--  التراجع (rollback) لو احتيج:
--   alter table outbox drop column claimed_at;
--  (لا حاجة حقيقية: عمود nullable شاغر مفيش سبب يخليه يترفض — لكن
--  متوفر للاكتمال.)
