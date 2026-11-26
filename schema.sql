-- =====================================================================
-- Project TITAN — Module 1 schema (MariaDB 10.5+)
--   mysql -u root -p < schema.sql
-- All timestamps are stored in UTC.
-- =====================================================================

CREATE DATABASE IF NOT EXISTS titan_db
  CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
USE titan_db;

-- ---- Uncomment to rebuild from scratch (DESTROYS ALL DATA) ----------
-- SET FOREIGN_KEY_CHECKS = 0;
-- DROP TABLE IF EXISTS anchor_receipts, audit_trail, telemetry_readings,
--                      medical_items, crates, dispatches, military_bases;
-- SET FOREIGN_KEY_CHECKS = 1;

CREATE TABLE IF NOT EXISTS military_bases (
  base_id           INT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,
  base_code         VARCHAR(16)  NOT NULL UNIQUE,
  base_name         VARCHAR(128) NOT NULL,
  sector_or_region  VARCHAR(64)  NOT NULL,
  latitude          DOUBLE       NOT NULL,
  longitude         DOUBLE       NOT NULL,
  geofence_radius_m INT UNSIGNED NOT NULL DEFAULT 500,
  CONSTRAINT ck_base_lat CHECK (latitude  BETWEEN -90  AND 90),
  CONSTRAINT ck_base_lng CHECK (longitude BETWEEN -180 AND 180)
) ENGINE=InnoDB;

CREATE TABLE IF NOT EXISTS dispatches (
  dispatch_id         INT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,
  vehicle_callsign    VARCHAR(32)  NOT NULL,
  driver_name         VARCHAR(96)  NOT NULL,
  origin_base_id      INT UNSIGNED NOT NULL,
  destination_base_id INT UNSIGNED NOT NULL,
  dispatched_at       DATETIME(3)  NULL,          -- set on first exit from origin geofence
  arrived_at          DATETIME(3)  NULL,          -- set on first entry into destination geofence
  status ENUM('PLANNED','EN_ROUTE','ARRIVED','CANCELLED') NOT NULL DEFAULT 'PLANNED',
  KEY ix_dispatch_status (status),
  CONSTRAINT fk_disp_origin FOREIGN KEY (origin_base_id)      REFERENCES military_bases(base_id),
  CONSTRAINT fk_disp_dest   FOREIGN KEY (destination_base_id) REFERENCES military_bases(base_id)
) ENGINE=InnoDB;

CREATE TABLE IF NOT EXISTS crates (
  crate_id            INT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,
  qr_code_payload     VARCHAR(255) NOT NULL UNIQUE,
  current_dispatch_id INT UNSIGNED NULL,
  current_base_id     INT UNSIGNED NULL,
  movement_status ENUM('AT_BASE','IN_TRANSIT','AT_BATTLEFIELD','IN_STORAGE','DISPENSED')
                  NOT NULL DEFAULT 'AT_BASE',
  integrity_status ENUM('SAFE','COMPROMISED_THERMAL','COMPROMISED_TAMPER','HASH_MISMATCH')
                  NOT NULL DEFAULT 'SAFE',
  is_locked_out       TINYINT(1)   NOT NULL DEFAULT 0,
  lockout_reason      VARCHAR(512) NULL,
  locked_out_at       DATETIME(3)  NULL,
  latitude            DOUBLE       NULL,
  longitude           DOUBLE       NULL,
  last_seen_at        DATETIME(3)  NULL,
  KEY ix_crate_movement (movement_status),
  KEY ix_crate_base (current_base_id),
  CONSTRAINT fk_crate_dispatch FOREIGN KEY (current_dispatch_id) REFERENCES dispatches(dispatch_id),
  CONSTRAINT fk_crate_base     FOREIGN KEY (current_base_id)     REFERENCES military_bases(base_id)
) ENGINE=InnoDB;

CREATE TABLE IF NOT EXISTS medical_items (
  item_id      INT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,
  crate_id     INT UNSIGNED NOT NULL,
  item_name    VARCHAR(128) NOT NULL,
  item_type    ENUM('VACCINE','BLOOD_PRODUCT','DRUG','MEDICAL_SUPPLY') NOT NULL,
  batch_number VARCHAR(64)  NOT NULL,
  quantity     INT UNSIGNED NOT NULL,
  unit         VARCHAR(16)  NOT NULL,
  expiry_date  DATE         NOT NULL,
  item_status  ENUM('AVAILABLE','QUARANTINED','EXPIRED','DISPENSED') NOT NULL DEFAULT 'AVAILABLE',
  KEY ix_item_crate (crate_id),
  KEY ix_item_expiry (expiry_date),
  KEY ix_item_type (item_type),
  CONSTRAINT fk_item_crate FOREIGN KEY (crate_id) REFERENCES crates(crate_id)
) ENGINE=InnoDB;

-- Raw edge telemetry (append-only). Burst-sync duplicates are rejected by uq_reading_seq.
CREATE TABLE IF NOT EXISTS telemetry_readings (
  reading_id    BIGINT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,
  crate_id      INT UNSIGNED NOT NULL,
  dispatch_id   INT UNSIGNED NULL,
  device_id     VARCHAR(64)  NOT NULL,
  sequence      BIGINT UNSIGNED NULL,
  recorded_at   DATETIME(3)  NOT NULL,            -- device clock (may be hours old after offline buffering)
  received_at   DATETIME(3)  NOT NULL,            -- server clock
  latitude      DOUBLE       NOT NULL,
  longitude     DOUBLE       NOT NULL,
  temperature_c DOUBLE       NOT NULL,
  humidity_pct  DOUBLE       NULL,
  battery_pct   DOUBLE       NULL,
  ldr_tamper    TINYINT(1)   NOT NULL DEFAULT 0,
  mkt_device_c  DOUBLE       NULL,
  mkt_server_c  DOUBLE       NULL,
  violations    VARCHAR(255) NULL,
  UNIQUE KEY uq_reading_seq (crate_id, device_id, sequence),
  KEY ix_reading_crate_time (crate_id, recorded_at),
  CONSTRAINT fk_reading_crate FOREIGN KEY (crate_id) REFERENCES crates(crate_id)
) ENGINE=InnoDB;

-- Per-crate SHA-256 hash chain.  current_hash = SHA256(previous_hash || event_json)
CREATE TABLE IF NOT EXISTS audit_trail (
  audit_id      BIGINT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,
  crate_id      INT UNSIGNED NOT NULL,
  chain_seq     INT UNSIGNED NOT NULL,            -- 1,2,3… per crate
  dispatch_id   INT UNSIGNED NULL,
  event_type    VARCHAR(32)  NOT NULL,
  event_json    MEDIUMTEXT   NOT NULL,            -- exact canonical JSON that was hashed
  previous_hash CHAR(64)     NOT NULL,            -- 64 zeros for the genesis record
  current_hash  CHAR(64)     NOT NULL,
  created_at    DATETIME(3)  NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
  UNIQUE KEY uq_audit_crate_seq (crate_id, chain_seq),
  UNIQUE KEY uq_audit_hash (current_hash),
  KEY ix_audit_type (event_type),
  CONSTRAINT fk_audit_crate FOREIGN KEY (crate_id) REFERENCES crates(crate_id)
) ENGINE=InnoDB;

-- External anchoring receipts (IPFS CID / Polygon tx hash, or mock receipts)
CREATE TABLE IF NOT EXISTS anchor_receipts (
  anchor_id    BIGINT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,
  crate_id     INT UNSIGNED NOT NULL,
  chain_length INT UNSIGNED NOT NULL,             -- anchored through this chain_seq
  head_hash    CHAR(64)     NOT NULL,             -- audit_trail.current_hash at chain_length
  provider     VARCHAR(32)  NOT NULL,             -- mock | ipfs | polygon
  network      VARCHAR(32)  NULL,
  ipfs_cid     VARCHAR(128) NULL,
  tx_hash      VARCHAR(80)  NULL,
  is_mock      TINYINT(1)   NOT NULL DEFAULT 0,
  anchored_at  DATETIME(3)  NOT NULL,
  KEY ix_anchor_crate (crate_id, chain_length),
  CONSTRAINT fk_anchor_crate FOREIGN KEY (crate_id) REFERENCES crates(crate_id)
) ENGINE=InnoDB;

-- ---------------------------------------------------------------------
-- Append-only enforcement at the database level
-- ---------------------------------------------------------------------
DELIMITER $$
DROP TRIGGER IF EXISTS trg_audit_no_update$$
CREATE TRIGGER trg_audit_no_update BEFORE UPDATE ON audit_trail FOR EACH ROW
BEGIN
  SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'audit_trail is append-only: UPDATE forbidden';
END$$
DROP TRIGGER IF EXISTS trg_audit_no_delete$$
CREATE TRIGGER trg_audit_no_delete BEFORE DELETE ON audit_trail FOR EACH ROW
BEGIN
  SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'audit_trail is append-only: DELETE forbidden';
END$$
DROP TRIGGER IF EXISTS trg_reading_no_update$$
CREATE TRIGGER trg_reading_no_update BEFORE UPDATE ON telemetry_readings FOR EACH ROW
BEGIN
  -- The app computes every derived column (mkt_server_c, violations) BEFORE the single INSERT,
  -- so it never needs UPDATE on this table.
  SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'telemetry_readings is append-only: UPDATE forbidden';
END$$
DELIMITER ;

-- ---------------------------------------------------------------------
-- Least-privilege application user (CHANGE THE PASSWORD).
-- TRUNCATE bypasses DELETE triggers, so the app user must not hold DROP/DELETE on audit tables.
-- ---------------------------------------------------------------------
CREATE USER IF NOT EXISTS 'titan'@'%' IDENTIFIED BY 'titan_pass';
GRANT SELECT, INSERT, UPDATE, DELETE ON titan_db.military_bases TO 'titan'@'%';
GRANT SELECT, INSERT, UPDATE, DELETE ON titan_db.dispatches     TO 'titan'@'%';
GRANT SELECT, INSERT, UPDATE, DELETE ON titan_db.crates         TO 'titan'@'%';
GRANT SELECT, INSERT, UPDATE, DELETE ON titan_db.medical_items  TO 'titan'@'%';
GRANT SELECT, INSERT                 ON titan_db.telemetry_readings TO 'titan'@'%';
GRANT SELECT, INSERT                 ON titan_db.audit_trail     TO 'titan'@'%';
GRANT SELECT, INSERT                 ON titan_db.anchor_receipts TO 'titan'@'%';
FLUSH PRIVILEGES;
