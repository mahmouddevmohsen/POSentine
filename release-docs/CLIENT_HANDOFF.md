# POSentine — Client Handoff

**For:** the business owner
**Not for:** developers — this is the plain-language explanation. Technical detail lives in `README.md`, `VERIFY.md`, and `READONLY_GUARANTEE.md`.

> **Note on language:** this master document is in English, per this project's existing pattern (`VERIFY.md` keeps its Arabic customer-facing summary marked as a reviewed draft, not free translation — customer-facing wording here should get the same review before being sent, not a quick machine translation).

---

## What POSentine is

A monitoring layer that sits alongside your existing POS system (HD Soft). It watches your sales, cash, and shift activity, and sends you a report by Telegram twice a day — after the morning shift and after the evening shift.

## What it does NOT do

- It does **not** replace your POS.
- It does **not** change anything on your till. Every layer of that guarantee is documented and independently tested on every install — see `READONLY_GUARANTEE.md` if you want the detail.
- It does **not** make decisions or send accusations. Every sentence it can say is a fixed, pre-written phrase — nothing is generated freely.

## What it monitors

- Sales, collections, deliveries, withdrawals, and returns, per shift.
- Cash counts, and whether they match what the system expects.
- Unusual events: a zero-price item, a refund, a cash difference, a deleted invoice, an unusually quiet period, or the till's own database growing large.
- Which staff member was active on the till during each shift.

## What the dashboard shows

A private web page (URL provided separately) with 7 views:

| Screen | What it shows |
|---|---|
| نظرة عامة (Overview) | The most recent completed shift — total, status, cash breakdown |
| الورديات (Shifts) | The last several days of completed shifts |
| يومية الخزينة (Cashbook) | The financial ledger, same order as your own cashbook |
| المستخدمون (Users) | Who was active during each shift |
| الأصناف (Products) | Item movement by quantity |
| المراقبة والصحة (Monitoring) | Whether the agent itself is running and reporting correctly |
| التنبيهات (Alerts) | Anything worth your attention, with plain-language reasons — never accusations |

The dashboard shows **either** a demo (clearly labeled "بيانات تجريبية — لا تمثل بيانات حقيقية") **or** your real data (labeled "بيانات حية من سجلات الرصد") — it never mixes the two, and it always tells you which one you're looking at.

## How to access the dashboard

1. Open the URL provided to you.
2. The first time, on the device/browser you'll use regularly, you'll be given a one-time setup step to switch it from demo to your real data. This only needs doing once per device.
3. After that, the page always opens showing your live data.

## What the agent does, day to day

Runs automatically every 3 minutes on the till, reads what changed, and uploads it. You do not need to start it, stop it, or check on it — it recovers on its own after a restart or a temporary internet drop. The one thing to know: **it only runs while your till account is logged in.** If the till is logged out, it pauses and picks back up automatically the moment someone logs back in — nothing is lost in between.

## Basic troubleshooting

| If you see... | What it means | What to do |
|---|---|---|
| The dashboard shows "بيانات تجريبية" when you expected real data | The one-time device setup step (above) hasn't been done on this device yet, or needs redoing | Contact support |
| "الوكيل متصل" (agent connected) is missing or shows an old timestamp | The till may be logged out, or offline | Check the till is logged in; it resumes automatically |
| Numbers on the dashboard don't match what you see on the till | This should never happen — the dashboard reads the exact same numbers your Telegram reports use | **Photograph both screens and contact support immediately.** Do not adjust anything yourself. |
| A Telegram report is late or missing | Could be a scheduling delay or a connectivity issue | Contact support if it's more than a few hours |

## Who to contact for support

**Phone / WhatsApp:** 01033052885
**Email:** mahmoudmohsen.work@gmail.com

---

## What this document deliberately leaves out

Per this project's own handling rules, this document does not include: any credential, connection string, or token; internal architecture beyond what a business owner needs; any forensic or internal audit detail; or developer commands that aren't relevant to daily use. Those live in the technical documentation and stay internal.
