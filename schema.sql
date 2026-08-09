        -- ============================================================
        --  POSentine — Supabase schema
        --  شغّله في: Supabase → SQL Editor → New query → Run
        --  آمن للتشغيل أكتر من مرة (idempotent)
        -- ============================================================
        --  قاعدة زمنية حاكمة:
        --    timestamp   (بدون منطقة) = وقت الـPOS المحلي، يتخزن زي ما هو
        --    timestamptz (بمنطقة)     = وقتنا إحنا (إنشاء/إرسال/heartbeat)
        --  خلطهم = أخطر bug في النظام. متخلطهمش.
        -- ============================================================

        create extension if not exists pgcrypto;

        -- ============================================================
        -- 1) العملاء والمصادر
        -- ============================================================

        create table if not exists tenants (
          id                  uuid primary key default gen_random_uuid(),
          slug                text unique not null,
          name                text not null,
          timezone            text not null default 'Africa/Cairo',
          shift_morning_start time not null default '07:00',
          shift_evening_start time not null default '19:00',
          currency            text not null default 'ج',
          go_live_at          timestamptz,          -- الأحداث الأقدم منه تتقمع
          created_at          timestamptz not null default now()
        );

        comment on column tenants.go_live_at is
          'أي حدث قبل التاريخ ده يتسجل status=suppressed ومايتبعتش';

        create table if not exists sources (
          id          uuid primary key default gen_random_uuid(),
          tenant_id   uuid not null references tenants(id) on delete cascade,
          slug        text not null,                -- main | delivery
          label       text not null,
          pos_vendor  text not null default 'hdsoft',
          active      boolean not null default true,
          created_at  timestamptz not null default now(),
          unique (tenant_id, slug)
        );

        -- المستقبلون (قناة + وجهة) — list مش قيمة واحدة
        create table if not exists recipients (
          id          bigserial primary key,
          tenant_id   uuid not null references tenants(id) on delete cascade,
          channel     text not null check (channel in ('telegram','whatsapp')),
          address     text not null,                -- chat_id أو رقم
          label       text,
          active      boolean not null default true,
          unique (tenant_id, channel, address)
        );

        -- مفاتيح تشغيل التنبيهات (كشف دايماً شغال، الإرسال بمفتاح)
        create table if not exists alert_settings (
          tenant_id   uuid not null references tenants(id) on delete cascade,
          alert_type  text not null,
          detect      boolean not null default true,
          notify      boolean not null default false,
          threshold   numeric(12,2),
          primary key (tenant_id, alert_type)
        );

        -- ============================================================
        -- 2) حالة المزامنة والنبض
        -- ============================================================

        create table if not exists sync_state (
          tenant_id            uuid not null references tenants(id) on delete cascade,
          source_id            uuid not null references sources(id) on delete cascade,
          watermark_salid      bigint not null default 0,
          watermark_saledeid   bigint not null default 0,
          rescan_from_salid    bigint not null default 0,   -- بداية نافذة الـ24 ساعة
          last_sync_at         timestamptz,
          last_rescan_at       timestamptz,
          pos_max_salid        bigint,
          restore_suspected    boolean not null default false,  -- 🔴 حماية الاسترجاع
          schema_ok            boolean not null default true,
          primary key (tenant_id, source_id)
        );

        comment on column sync_state.restore_suspected is
          'يتحط true لو MAX(salid) في الـPOS أقل من الـwatermark — يوقف المزامنة ويستنى تدخل يدوي';

        create table if not exists heartbeats (
          id             bigserial primary key,
          tenant_id      uuid not null references tenants(id) on delete cascade,
          source_id      uuid not null references sources(id) on delete cascade,
          at             timestamptz not null default now(),
          agent_version  text,
          pos_clock      timestamp,        -- ساعة جهاز الـPOS
          drift_seconds  integer,          -- الفرق عن ساعتنا
          rows_pulled    integer,
          ok             boolean not null default true,
          note           text
        );

        create index if not exists ix_heartbeats_recent
          on heartbeats (tenant_id, source_id, at desc);

        -- ============================================================
        -- 3) لقطات مرجعية (الأسماء والأسعار تتجمّد)
        -- ============================================================

        create table if not exists pos_users (
          tenant_id   uuid not null,
          source_id   uuid not null,
          uid         bigint not null,
          name        text,                 -- null → "مستخدم محذوف #uid"
          updated_at  timestamptz not null default now(),
          primary key (tenant_id, source_id, uid)
        );

        create table if not exists pos_products (
          tenant_id   uuid not null,
          source_id   uuid not null,
          itid        bigint not null,      -- 🔑 Items.Itid — مش itcode
          itcode      text,
          itname      text,
          list_price  numeric(12,2),
          main_cat    integer,
          sub_cat     integer,
          is_modifier boolean generated always as (coalesce(list_price,0) = 0) stored,
          updated_at  timestamptz not null default now(),
          primary key (tenant_id, source_id, itid)
        );

        comment on column pos_products.is_modifier is
          'سعر القائمة صفر = ملاحظة مطبخ مش منتج (شطة حمراء، بدون دقة...) — تتشال من أعلى الأصناف';

        -- ============================================================
        -- 4) الفواتير
        -- ============================================================

        create table if not exists invoices (
          tenant_id      uuid not null,
          source_id      uuid not null,
          salid          bigint not null,
          receipt_num    bigint,
          sold_at        timestamp not null,        -- وقت الـPOS المحلي
          total          numeric(12,2) not null default 0,
          delivery_cost  numeric(12,2) not null default 0,
          user_uid       bigint,
          cust_code      integer,
          sal_t          integer,
          sal_type       integer,
          sale_rtype     integer,
          kind           text not null check (kind in ('cash','external','return','other')),
          first_seen_at  timestamptz not null default now(),
          last_seen_at   timestamptz not null default now(),
          deleted_at     timestamptz,               -- اتأكد غيابه من نافذة الرصد
          primary key (tenant_id, source_id, salid)
        );

        comment on column invoices.kind is
          'الترتيب إلزامي: return أولاً (saleRtype=1 أو saltype=1)، بعدين salT=1→cash، salT=2→external، غير كده→other';
        comment on column invoices.total is
          'saltot كما هو = مجموع السطور + delivery_cost';

        create index if not exists ix_invoices_shift
          on invoices (tenant_id, source_id, sold_at)
          where deleted_at is null;

        create index if not exists ix_invoices_user
          on invoices (tenant_id, source_id, user_uid, sold_at)
          where deleted_at is null;

        create index if not exists ix_invoices_zero
          on invoices (tenant_id, source_id, sold_at)
          where total = 0 and deleted_at is null;

        create table if not exists invoice_lines (
          tenant_id    uuid not null,
          source_id    uuid not null,
          saledeid     bigint not null,
          salid        bigint not null,
          itid         bigint,
          item_name    text,                  -- ❄️ مجمّد وقت المزامنة
          list_price   numeric(12,2),         -- ❄️ مجمّد — أساس كشف الصنف المجاني
          qty          numeric(12,3) not null default 0,
          line_total   numeric(12,2) not null default 0,   -- saleprice = إجمالي السطر
          sold_at      timestamp,
          first_seen_at timestamptz not null default now(),
          primary key (tenant_id, source_id, saledeid)
        );

        comment on column invoice_lines.line_total is
          'SalesDe.saleprice = إجمالي السطر وليس سعر الوحدة. سعر الوحدة = line_total / qty';
        comment on column invoice_lines.list_price is
          'مجمّد وقت المزامنة عشان الكشف التاريخي مايتغيرش لو عدّلوا سعر الصنف';

        create index if not exists ix_lines_invoice
          on invoice_lines (tenant_id, source_id, salid);

        create index if not exists ix_lines_item
          on invoice_lines (tenant_id, source_id, itid, sold_at);

        -- سطر مجاني: صنف مسعّر اتباع بصفر
        create index if not exists ix_lines_free
          on invoice_lines (tenant_id, source_id, salid)
          where line_total = 0 and list_price > 0;

        -- ============================================================
        -- 5) الخزينة
        -- ============================================================

        create table if not exists cash_counts (
          tenant_id    uuid not null,
          source_id    uuid not null,
          srid         bigint not null,
          counted_at   timestamp not null,
          user_uid     bigint,
          user_value   numeric(12,2),
          app_value    numeric(12,2),
          diff_value   numeric(12,2),
          pc_name      text,
          kind         text not null check (kind in ('no_count','real_count')),
          primary key (tenant_id, source_id, srid)
        );

        comment on column cash_counts.kind is
          '🚨 SrUserval = 0 → no_count (الكاشير مدخلش رقم). ممنوع اعتبارها عجز.';

        create index if not exists ix_cash_counts_time
          on cash_counts (tenant_id, source_id, counted_at);

        -- ============================================================
        -- 6) الأحداث والإرسال
        -- ============================================================

        create table if not exists events (
          id           bigserial primary key,
          tenant_id    uuid not null references tenants(id) on delete cascade,
          source_id    uuid not null references sources(id) on delete cascade,
          type         text not null,
          dedup_key    text not null,
          level        smallint not null check (level between 1 and 3),
          occurred_at  timestamp not null,
          payload      jsonb not null default '{}'::jsonb,
          status       text not null default 'detected'
                      check (status in ('detected','suppressed','queued','reported')),
          created_at   timestamptz not null default now(),
          unique (tenant_id, source_id, type, dedup_key)
        );

        comment on table events is
          'الكشف دايماً شغال. الإرسال بيتحكم فيه alert_settings.notify + go_live_at';

        create index if not exists ix_events_open
          on events (tenant_id, source_id, occurred_at desc)
          where status in ('detected','queued');

        create table if not exists outbox (
          id          bigserial primary key,
          tenant_id   uuid not null references tenants(id) on delete cascade,
          channel     text not null check (channel in ('telegram','whatsapp')),
          recipient   text not null,
          kind        text not null,       -- shift_report | monthly_products | alert
          body        text not null,
          event_id    bigint references events(id) on delete set null,
          dedup_key   text not null,
          status      text not null default 'pending'
                      check (status in ('pending','sending','sent','failed','dead')),
          attempts    smallint not null default 0,
          last_error  text,
          created_at  timestamptz not null default now(),
          sent_at     timestamptz,
          unique (tenant_id, channel, recipient, dedup_key)
        );

        comment on table outbox is
          'الحالة: pending → sending → sent | failed(retry ≤3) → dead. التسجيل بعد نجاح الإرسال.';

        create index if not exists ix_outbox_queue
          on outbox (status, created_at)
          where status in ('pending','failed');

        -- ============================================================
        -- 7) سجل التقارير + الشذوذ الداخلي
        -- ============================================================

        create table if not exists shift_reports (
          tenant_id      uuid not null,
          source_id      uuid not null,
          shift_date     date not null,
          shift_name     text not null check (shift_name in ('morning','evening')),
          window_start   timestamp not null,
          window_end     timestamp not null,
          primary_user   text,
          sales          numeric(12,2),
          returns        numeric(12,2),
          delivery       numeric(12,2),
          collections    numeric(12,2),
          grand_total    numeric(12,2),
          n_cash         integer,
          n_return       integer,
          n_external     integer,
          is_partial     boolean not null default false,   -- أول وردية بعد التركيب
          generated_at   timestamptz not null default now(),
          primary key (tenant_id, source_id, shift_date, shift_name)
        );

        comment on column shift_reports.grand_total is
          'يومية الخزينة: sales + collections − returns − delivery  (متحقق: 19,205)';
        comment on column shift_reports.is_partial is
          'الوردية الجارية وقت التركيب — تتسجل ومايتبعتش عنها تقرير';

        -- أي حاجة غير متوقعة نشوفها إحنا بس
        create table if not exists internal_anomalies (
          id          bigserial primary key,
          tenant_id   uuid,
          source_id   uuid,
          kind        text not null,   -- unknown_salT | schema_drift | restore_suspected
                                      -- clock_drift | date_change_log | dead_man | telegram_403
          detail      jsonb not null default '{}'::jsonb,
          created_at  timestamptz not null default now(),
          resolved_at timestamptz
        );

        -- ============================================================
        -- 8) RLS — عزل العملاء من اليوم الأول
        -- ============================================================

        alter table tenants            enable row level security;
        alter table sources            enable row level security;
        alter table recipients         enable row level security;
        alter table alert_settings     enable row level security;
        alter table sync_state         enable row level security;
        alter table heartbeats         enable row level security;
        alter table pos_users          enable row level security;
        alter table pos_products       enable row level security;
        alter table invoices           enable row level security;
        alter table invoice_lines      enable row level security;
        alter table cash_counts        enable row level security;
        alter table events             enable row level security;
        alter table outbox             enable row level security;
        alter table shift_reports      enable row level security;
        alter table internal_anomalies enable row level security;

        -- الأجينت: JWT مخصص فيه claim اسمه tenant_id — يكتب في جداول الاستيعاب فقط
        do $$
        declare t text;
        begin
          foreach t in array array[
            'sync_state','heartbeats','pos_users','pos_products',
            'invoices','invoice_lines','cash_counts'
          ] loop
            execute format($f$
              drop policy if exists agent_rw on public.%I;
              create policy agent_rw on public.%I
                for all
                using      ((auth.jwt() ->> 'tenant_id')::uuid = tenant_id)
                with check ((auth.jwt() ->> 'tenant_id')::uuid = tenant_id);
            $f$, t, t);
          end loop;
        end $$;

        -- ملاحظة: service_role بيتخطى RLS تلقائياً — بيتستخدم من GitHub Actions فقط،
        -- وممنوع منعاً باتاً يتحط على جهاز العميل.

        -- ============================================================
        -- 9) بيانات العميل الأول
        -- ============================================================

        insert into tenants (slug, name, timezone)
        values ('sobh_onthefast', 'On The Fast — Sobh', 'Africa/Cairo')
        on conflict (slug) do nothing;

        insert into sources (tenant_id, slug, label, pos_vendor)
        select id, 'main', 'الكاشير الأساسي', 'hdsoft' from tenants where slug = 'sobh_onthefast'
        on conflict (tenant_id, slug) do nothing;

        -- المرحلة الأولى: التقرير لمحمود، والمالك يتضاف بعدين
        insert into recipients (tenant_id, channel, address, label)
        select id, 'telegram', '6888195170', 'محمود — تجربة' from tenants where slug = 'sobh_onthefast'
        on conflict do nothing;

        -- الكشف شغال من أول يوم، والإرسال مقفول لحد ما نطمن
        insert into alert_settings (tenant_id, alert_type, detect, notify, threshold)
        select t.id, a.typ, true, a.notify, a.thr
        from tenants t,
            (values
                ('zero_invoice',     false, null::numeric),
                ('refund',           false, 500),
                ('cash_diff',        false, 500),
                ('deleted_invoice',  false, null),
                ('no_sales',         false, null),
                ('db_size',          false, null)
            ) as a(typ, notify, thr)
        where t.slug = 'sobh_onthefast'
        on conflict (tenant_id, alert_type) do nothing;

        insert into sync_state (tenant_id, source_id)
        select s.tenant_id, s.id from sources s
        join tenants t on t.id = s.tenant_id
        where t.slug = 'sobh_onthefast'
        on conflict do nothing;
