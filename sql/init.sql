-- ============================================================
-- Boss 招聘采集库 初始化脚本
-- 目标库: boss_recruit
-- 约定: 主库可写，从库只读；应用只连接主库
-- 注意: 必须以 utf8mb4 导入，否则中文注释会变成乱码
--       mysql --default-character-set=utf8mb4 < init.sql
-- ============================================================

SET NAMES utf8mb4;
SET FOREIGN_KEY_CHECKS = 0;

CREATE DATABASE IF NOT EXISTS boss_recruit
  DEFAULT CHARACTER SET utf8mb4
  DEFAULT COLLATE utf8mb4_unicode_ci;

USE boss_recruit;

-- ------------------------------------------------------------
-- 采集任务表: 记录一次采集任务的入参、状态与结果统计
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS tasks (
  task_id      VARCHAR(64)  NOT NULL                COMMENT '任务唯一标识(12位十六进制)',
  mode         VARCHAR(20)  NOT NULL                COMMENT '采集模式: recommend=推荐职位, search=关键词搜索',
  keyword      VARCHAR(255) NOT NULL DEFAULT ''     COMMENT '搜索关键词, 推荐模式为空',
  city         VARCHAR(32)  NOT NULL DEFAULT ''     COMMENT '城市编码, 如 100010000; 为空时使用抓包默认值',
  pages        INT          NOT NULL DEFAULT 1      COMMENT '计划采集页数, 1-10',
  page_size    INT          NOT NULL DEFAULT 15     COMMENT '每页请求条数, 最大 15',
  detail_limit INT          NOT NULL DEFAULT 0      COMMENT '额外抓取的职位详情条数, 0-20',
  detail_interval FLOAT     NOT NULL DEFAULT 60     COMMENT '详情请求间隔秒数, 30-600',
  detail_ok    INT          NOT NULL DEFAULT 0      COMMENT '详情成功入库条数',
  detail_fail  INT          NOT NULL DEFAULT 0      COMMENT '详情失败条数',
  detail_status VARCHAR(16) NOT NULL DEFAULT 'none' COMMENT 'none/partial/full/blocked',
  account_id   VARCHAR(32)  NOT NULL DEFAULT ''     COMMENT '执行该任务所用的账号ID; 空为旧任务/轮询',
  account_label VARCHAR(64) NOT NULL DEFAULT ''     COMMENT '执行账号的备注名快照',
  status       VARCHAR(20)  NOT NULL                COMMENT '任务状态: queued/running/success/failed',
  message      TEXT         NULL                    COMMENT '状态说明或错误详情',
  rows_count   INT          NOT NULL DEFAULT 0      COMMENT '成功入库的职位条数',
  created_at   DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '任务创建时间',
  finished_at  DATETIME     NULL                    COMMENT '任务结束时间',
  PRIMARY KEY (task_id),
  KEY idx_tasks_status (status),
  KEY idx_tasks_created_at (created_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
  COMMENT='采集任务表: 记录每次采集任务的入参、状态与结果统计';

-- ------------------------------------------------------------
-- 职位表: 采集到的招聘职位明细, 同一任务内按 lid 去重
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS jobs (
  id              BIGINT UNSIGNED NOT NULL AUTO_INCREMENT COMMENT '自增主键',
  task_id         VARCHAR(64)  NOT NULL                COMMENT '所属采集任务 ID, 关联 tasks.task_id',
  job_name        VARCHAR(255) NOT NULL DEFAULT ''     COMMENT '职位名称',
  salary_desc     VARCHAR(100) NOT NULL DEFAULT ''     COMMENT '薪资描述原文, 未登录时为空',
  job_experience  VARCHAR(100) NOT NULL DEFAULT ''     COMMENT '经验要求, 如 3-5年',
  job_degree      VARCHAR(100) NOT NULL DEFAULT ''     COMMENT '学历要求, 如 本科',
  city_name       VARCHAR(100) NOT NULL DEFAULT ''     COMMENT '工作城市',
  area_district   VARCHAR(100) NOT NULL DEFAULT ''     COMMENT '区域名称, 如 浦东新区',
  business_district VARCHAR(100) NOT NULL DEFAULT ''   COMMENT '商圈名称',
  brand_name      VARCHAR(255) NOT NULL DEFAULT ''     COMMENT '公司名称',
  brand_industry  VARCHAR(255) NOT NULL DEFAULT ''     COMMENT '公司行业',
  brand_scale_name VARCHAR(100) NOT NULL DEFAULT ''    COMMENT '公司规模',
  boss_name       VARCHAR(100) NOT NULL DEFAULT ''     COMMENT '招聘者称呼',
  boss_title      VARCHAR(100) NOT NULL DEFAULT ''     COMMENT '招聘者职位头衔',
  skills          TEXT         NULL                    COMMENT '技能标签, 多值以 | 分隔',
  job_labels      TEXT         NULL                    COMMENT '职位标签, 多值以 | 分隔',
  post_description TEXT        NULL                    COMMENT '职位描述正文',
  address         TEXT         NULL                    COMMENT '详细工作地址',
  encrypt_job_id  VARCHAR(255) NOT NULL DEFAULT ''     COMMENT '职位加密 ID, 用于拼接详情页链接',
  lid             VARCHAR(255) NOT NULL DEFAULT ''     COMMENT '列表接口返回的职位标识, 任务内去重依据',
  security_id     TEXT         NULL                    COMMENT '详情接口所需的一次性安全参数',
  detail_url      TEXT         NULL                    COMMENT '职位详情页完整链接',
  created_at      DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '入库时间',
  PRIMARY KEY (id),
  UNIQUE KEY uk_jobs_task_lid (task_id, lid),
  KEY idx_jobs_job_name (job_name),
  KEY idx_jobs_brand_name (brand_name),
  KEY idx_jobs_city_name (city_name),
  KEY idx_jobs_created_at (created_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
  COMMENT='职位表: 采集到的招聘职位明细, 同一任务内按 lid 去重';

-- ------------------------------------------------------------
-- 健康检查表: 记录长时间运行中接口可用性与 token 存活情况
-- ------------------------------------------------------------
-- ------------------------------------------------------------
-- 账号表: 多用户凭证的元数据（Cookie 本体不入库，存 data/accounts/）
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS accounts (
  account_id    VARCHAR(32)  NOT NULL                COMMENT '账号唯一ID(短UUID)',
  label         VARCHAR(64)  NOT NULL                COMMENT '用户备注名',
  boss_user_id  VARCHAR(64)  NOT NULL DEFAULT ''     COMMENT 'BOSS返回的真实userId',
  cdp_port      INT          NOT NULL                COMMENT '该账号专属CDP调试端口',
  status        VARCHAR(16)  NOT NULL DEFAULT 'expired' COMMENT '凭证状态: active/expired',
  last_probe_at DATETIME     NULL                    COMMENT '最近一次探活时间',
  last_used_at  DATETIME     NULL                    COMMENT '最近一次被任务使用时间(轮询依据)',
  detail_blocked_until DATETIME NULL                 COMMENT '详情风控冷却截止时间',
  created_at    DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
  PRIMARY KEY (account_id),
  KEY idx_accounts_status (status)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
  COMMENT='账号表: 多用户凭证元数据';

-- ------------------------------------------------------------
-- 职位详情版本表: detail.json 全量字段, 按 lid 关联 jobs
--   内容未变化 -> 刷新 captured_at / 易变字段; 内容变化 -> 新增一行
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS job_details (
  id                BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  lid               VARCHAR(255) NOT NULL                COMMENT '关联 jobs.lid',
  task_id           VARCHAR(64)  NOT NULL DEFAULT ''      COMMENT '产生本次快照的任务',
  content_hash      CHAR(64)     NOT NULL DEFAULT ''      COMMENT '业务字段规范化 JSON 的 sha256',
  job_name          VARCHAR(255) NOT NULL DEFAULT ''      COMMENT '职位名称',
  position_name     VARCHAR(255) NOT NULL DEFAULT ''      COMMENT '职能名称',
  salary_desc       VARCHAR(100) NOT NULL DEFAULT ''      COMMENT '薪资描述',
  experience_name   VARCHAR(100) NOT NULL DEFAULT ''      COMMENT '经验要求',
  degree_name       VARCHAR(100) NOT NULL DEFAULT ''      COMMENT '学历要求',
  location_name     VARCHAR(100) NOT NULL DEFAULT ''      COMMENT '城市',
  address           VARCHAR(500) NOT NULL DEFAULT ''      COMMENT '详细地址',
  longitude         DECIMAL(10,6) NULL                    COMMENT '经度',
  latitude          DECIMAL(10,6) NULL                    COMMENT '纬度',
  static_map_url    VARCHAR(500) NOT NULL DEFAULT ''      COMMENT '静态地图',
  skills            TEXT NULL                             COMMENT '技能标签, | 分隔',
  job_status_desc   VARCHAR(64) NOT NULL DEFAULT ''       COMMENT '招聘状态描述',
  job_valid         TINYINT(1) NOT NULL DEFAULT 1         COMMENT '是否有效(1-有效)',
  anonymous         TINYINT(1) NOT NULL DEFAULT 0         COMMENT '是否匿名职位',
  post_description  MEDIUMTEXT NULL                       COMMENT '职位描述正文',
  boss_name         VARCHAR(100) NOT NULL DEFAULT ''      COMMENT '招聘者称呼',
  boss_title        VARCHAR(100) NOT NULL DEFAULT ''      COMMENT '招聘者头衔',
  boss_avatar       VARCHAR(500) NOT NULL DEFAULT ''      COMMENT '招聘者头像',
  boss_active_desc  VARCHAR(64) NOT NULL DEFAULT ''       COMMENT '活跃描述(易变,不参与哈希)',
  boss_online       TINYINT(1) NOT NULL DEFAULT 0         COMMENT '是否在线(易变)',
  boss_certificated TINYINT(1) NOT NULL DEFAULT 0         COMMENT '是否认证',
  brand_name        VARCHAR(255) NOT NULL DEFAULT ''      COMMENT '公司名称',
  brand_logo        VARCHAR(500) NOT NULL DEFAULT ''      COMMENT '公司 logo',
  brand_stage       VARCHAR(64) NOT NULL DEFAULT ''       COMMENT '融资阶段',
  brand_scale       VARCHAR(64) NOT NULL DEFAULT ''       COMMENT '公司规模',
  brand_industry    VARCHAR(255) NOT NULL DEFAULT ''      COMMENT '公司行业',
  brand_introduce   MEDIUMTEXT NULL                       COMMENT '公司介绍',
  brand_labels      TEXT NULL                             COMMENT '福利标签, | 分隔',
  brand_active_at   DATETIME NULL                         COMMENT '公司活跃时间(易变,不参与哈希)',
  encrypt_brand_id  VARCHAR(128) NOT NULL DEFAULT ''      COMMENT '加密公司ID',
  encrypt_job_id    VARCHAR(255) NOT NULL DEFAULT ''      COMMENT '跨会话稳定的职位ID, 去重用',
  security_id       VARCHAR(512) NOT NULL DEFAULT ''      COMMENT '本次请求用的 securityId',
  raw_json          LONGTEXT NULL                         COMMENT '完整 zpData, 便于补字段',
  first_captured_at DATETIME(3) NOT NULL                  COMMENT '该版本首次采集时间',
  captured_at       DATETIME(3) NOT NULL                  COMMENT '最近一次观测时间',
  capture_count     INT NOT NULL DEFAULT 1                COMMENT '该版本被观测次数',
  created_at        DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (id),
  UNIQUE KEY uk_lid_captured (lid, captured_at),
  KEY idx_lid_hash (lid, content_hash),
  KEY idx_detail_task (task_id),
  KEY idx_detail_brand (brand_name),
  KEY idx_detail_encrypt (encrypt_job_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
  COMMENT='职位详情版本快照; 同 lid 内容变化才新增行';

-- ------------------------------------------------------------
-- 详情请求日志: 配额统计与健康观测
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS detail_requests (
  id           BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  account_id   VARCHAR(32) NOT NULL DEFAULT '',
  task_id      VARCHAR(64) NOT NULL DEFAULT '',
  lid          VARCHAR(255) NOT NULL DEFAULT '',
  requested_at DATETIME NOT NULL,
  api_code     INT NOT NULL DEFAULT 0,
  ok           TINYINT(1) NOT NULL DEFAULT 0,
  note         VARCHAR(255) NOT NULL DEFAULT '',
  PRIMARY KEY (id),
  KEY idx_detail_req_at (requested_at),
  KEY idx_detail_req_account (account_id, requested_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
  COMMENT='详情请求日志';

CREATE TABLE IF NOT EXISTS health_checks (
  id            BIGINT UNSIGNED NOT NULL AUTO_INCREMENT COMMENT '自增主键',
  probe_at      DATETIME     NOT NULL                COMMENT '探测时间',
  api_code      INT          NOT NULL                COMMENT '接口返回码, 0 表示正常',
  api_message   VARCHAR(255) NOT NULL DEFAULT ''     COMMENT '接口返回信息',
  token_age_min DECIMAL(10,2) NOT NULL DEFAULT 0    COMMENT '本次使用的 stoken 已存活分钟数',
  token_len     INT          NOT NULL DEFAULT 0      COMMENT 'stoken 长度',
  refreshed     TINYINT(1)   NOT NULL DEFAULT 0      COMMENT '本次探测前是否重新生成了 stoken',
  ok            TINYINT(1)   NOT NULL DEFAULT 0      COMMENT '本次探测是否通过',
  note          VARCHAR(255) NOT NULL DEFAULT ''     COMMENT '备注, 如异常原因',
  PRIMARY KEY (id),
  KEY idx_health_probe_at (probe_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
  COMMENT='健康检查表: 记录长时间运行中接口可用性与 token 存活情况';
