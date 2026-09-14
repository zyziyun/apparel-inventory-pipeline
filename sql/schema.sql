-- 服装库存数据模型，抄自 ERPNext 的 item / item_variant / stock_ledger_entry / bin
DROP SCHEMA IF EXISTS analytics CASCADE;
CREATE SCHEMA analytics;
SET search_path = analytics;

CREATE TABLE style (
    style_id    TEXT PRIMARY KEY,
    style_name  TEXT NOT NULL,
    category    TEXT,
    season      TEXT
);

-- 尺码是有序的，字典序是错的，所以需要一张排序表
CREATE TABLE attribute_value (
    attribute   TEXT NOT NULL,
    value       TEXT NOT NULL,
    sort_order  INT  NOT NULL,
    PRIMARY KEY (attribute, value)
);

CREATE TABLE sku (
    sku_id    TEXT PRIMARY KEY,
    style_id  TEXT NOT NULL REFERENCES style(style_id),
    color     TEXT NOT NULL,
    size      TEXT NOT NULL,
    UNIQUE (style_id, color, size)
);

CREATE TABLE warehouse (
    warehouse_id TEXT PRIMARY KEY,
    name         TEXT NOT NULL
);

-- 流水，只增不改。对应 ERPNext 的 stock_ledger_entry
CREATE TABLE stock_ledger_entry (
    entry_id              BIGSERIAL PRIMARY KEY,
    sku_id                TEXT NOT NULL REFERENCES sku(sku_id),
    warehouse_id          TEXT NOT NULL REFERENCES warehouse(warehouse_id),
    posting_ts            TIMESTAMPTZ NOT NULL,
    qty_delta             NUMERIC(18,4) NOT NULL,   -- ERPNext 用 Float，我们用 NUMERIC
    qty_after_transaction NUMERIC(18,4) NOT NULL,   -- 物化的 running balance
    voucher_type          TEXT NOT NULL,            -- 多态外键，数据库层约束不了
    voucher_no            TEXT NOT NULL,
    is_cancelled          BOOLEAN NOT NULL DEFAULT FALSE
);

CREATE INDEX idx_sle_active ON stock_ledger_entry (sku_id, warehouse_id, posting_ts)
    WHERE is_cancelled = FALSE;                     -- 部分索引

-- 汇总表，流水的物化聚合。对应 ERPNext 的 bin
CREATE TABLE bin (
    sku_id       TEXT NOT NULL REFERENCES sku(sku_id),
    warehouse_id TEXT NOT NULL REFERENCES warehouse(warehouse_id),
    actual_qty   NUMERIC(18,4) NOT NULL DEFAULT 0,  -- on hand
    reserved_qty NUMERIC(18,4) NOT NULL DEFAULT 0,  -- allocated
    ordered_qty  NUMERIC(18,4) NOT NULL DEFAULT 0,  -- 在途
    updated_at   TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (sku_id, warehouse_id)              -- ON CONFLICT 需要这个唯一约束
);

-- 第二个系统，用来做跨系统对账
CREATE TABLE wms_inventory (
    sku_id       TEXT NOT NULL,
    warehouse_id TEXT NOT NULL,
    qty          NUMERIC(18,4) NOT NULL,
    PRIMARY KEY (sku_id, warehouse_id)
);

-- 对账结果，带 run_id 才能看趋势
CREATE TABLE recon_result (
    run_id           BIGINT NOT NULL,
    rule_id          TEXT NOT NULL,
    severity         TEXT NOT NULL,
    entity_key       JSONB NOT NULL,
    expected         NUMERIC(18,4),
    actual           NUMERIC(18,4),
    delta            NUMERIC(18,4),
    detail           TEXT,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- 增量加载的水位
CREATE TABLE etl_watermark (
    source_name  TEXT PRIMARY KEY,
    watermark    TIMESTAMPTZ NOT NULL
);
