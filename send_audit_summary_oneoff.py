# -*- coding: utf-8 -*-
"""
send_audit_summary_oneoff.py — ONE-OFF: enqueue the 2026-08-11 forensic
false-green audit summary onto the existing outbox, so it goes out through
the real delivery path (outbox -> notifier claim -> Telegram), not a
bespoke send.

Not part of the delivery closure. Never imported by orchestrator.py,
notifier/telegram.py, or delivery.py. Issues exactly one insert_ignore call
against `outbox` — idempotent (unique dedup_key), safe to re-run, cannot
duplicate-send. No POS connection, no writes anywhere else. Meant to be run
once via .github/workflows/audit-summary-oneoff.yml and then deleted.
"""

from __future__ import annotations

import os

import events as E
import supa

DEDUP_KEY = "audit_summary:2026-08-11"

BODY = """\
📋 تقرير تدقيق POSentine — 2026-08-11

✅ التدقيق الجنائي الكامل اكتمل (مش مجرد "تمام")

1️⃣ فرق 1,140 ج: مش باج. المعادلة الحقيقية "مبيعات + مقبوضات − مرتجع − دليفري" \
بتطابق الرقمين المُرسلين بالظبط، مش جمع بسيط زي ما ظهر شكليًا.

2️⃣ باج حقيقي اتكشف وهو شغال فعليًا لحد قبل شوية: تقارير "🟢 الوردية مستقرة" \
كانت بتتبعت لورديات قبل ما الإيجنت يترّكب خالص — صفر فواتير حقيقي لأنه مفيش \
backfill بالتصميم. مُثبت من بيانات Supabase الحقيقية: 3 تقارير فاضية اتبعتت \
فعلاً (شات التجربة بس، مش المالك — go_live_at لسه NULL).

✅ تم الإصلاح ودفعه (main، 28cdc72 + d58c8b7). 433 اختبار ناجح (كانوا 418)، \
و31 اختبار Golden زي ما هم بالظبط بدون تغيير.

❓ إجابة السؤال الأساسي:
• تمييز البيانات التاريخية عن الوردية الحالية؟ أيوه، اتصلح.
• تمييز اليومي عن الوردية؟ ده كان سليم من الأساس (اتأكد بالتدقيق).
• رفض تقرير غير متسق حسابيًا؟ المعادلة سليمة دايمًا؛ مفيش قيد CHECK في \
قاعدة البيانات نفسها (محدودية موثّقة، مش خطر فعلي).
• منع بيانات جزئية/قديمة من تصنيف "مستقرة"؟ أيوه لحالة الصفر الكامل؛ لسه \
لأ لحالة انقطاع جزئي وسط الوردية (محدودية موثّقة، مش مُصلحة في الجولة دي).
• منع تكرار النوع ده بصمت؟ أيوه للحالة اللي حصلت واتثبتت فعلاً.

⚠️ مخاطر متبقية ومحتاجة قرارك: التقرير الكامل بكل التفاصيل (25 قسم) — \
reports/phase2/AUG11_FALSE_GREEN_AUDIT.md في الريبو."""


def main() -> int:
    E.assert_no_accusation(BODY)   # same guard every owner-facing send passes

    url = os.environ["SUPABASE_URL"]
    key = os.environ["SUPABASE_SERVICE_ROLE_KEY"]
    tenant_id = os.environ["TENANT_ID"]
    client = supa.Supa(url, anon_key=key, token=key)

    recipients = client.select("recipients", {
        "tenant_id": f"eq.{tenant_id}", "channel": "eq.telegram",
        "active": "eq.true", "notify_before_golive": "eq.true",
        "select": "address,label"})
    if not recipients:
        print("no eligible (active, notify_before_golive) telegram recipient found "
              "— refusing to enqueue for an owner recipient by accident")
        return 1

    rows = [{
        "tenant_id": tenant_id,
        "channel": "telegram",
        "recipient": r["address"],
        # NOT "alert": the notifier's gate_check() re-checks alert_settings.
        # notify for kind=="alert" rows, keyed by a type parsed out of the
        # dedup_key — that gate is for the six registered alert types
        # (zero_invoice, refund, ...), not a one-off admin message, and
        # correctly blocked this the first time it was tried with kind=
        # "alert"/dedup "alert:audit_summary:...". A distinct kind skips
        # that check entirely while still passing the recipient-level gates
        # (active / go_live_at / notify_before_golive), which are the ones
        # that actually matter here.
        "kind": "audit_summary",
        "body": BODY,
        "dedup_key": DEDUP_KEY,
        "status": "pending",
        "attempts": 0,
    } for r in recipients]

    client.insert_ignore("outbox", rows,
                         on_conflict="tenant_id,channel,recipient,dedup_key")
    print(f"enqueued (or already existed) for {len(rows)} recipient(s), "
          f"dedup_key={DEDUP_KEY}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
