export interface Status {
  listen: string;
  proxy: string;
  nodes: number;
  healthy_nodes: number;
  users: number;
  revoked_users: number;
  sing_box_alive: boolean;
  locked?: boolean;
  /** 手动重载模式下：数据库/磁盘与运行中的实例不一致（提示"配置未生效"） */
  config_dirty?: boolean;
}

export interface NodeRow {
  tag: string;
  name: string;
  type?: string;
  server?: string;
  port?: number | string;
  healthy: boolean;
  /** 有多少个 active 用户的节点池包含这个节点（池可重叠，可能大于用户总数） */
  users?: number;
}

/** 节点池里的一个成员；池内数组顺序即优先级。 */
export interface PoolNode {
  tag: string;
  name: string;
  healthy: boolean;
}

/** 轻量节点条目，只用于「节点池」编辑器的下拉。 */
export interface NodeOption {
  tag: string;
  name: string;
  healthy: boolean;
}

export interface UserRow {
  username: string;
  status: string;
  /** 节点池，按优先级排列，可为空数组 */
  nodes: PoolNode[];
  /** 当前实际走的是池里哪个节点（由后端从 Clash API 取，空串=未知） */
  active_node?: string;
  created_at: string;
  revoked_at?: string | null;
  proxy: string;
}

export interface CreatedUser {
  username: string;
  password: string;
  nodes: PoolNode[];
  proxy: string;
}

export interface ImportResult {
  ok: boolean;
  /** 本次解析出的节点数 */
  count: number;
  /** 新加入的节点数 */
  added: number;
  /** 覆盖更新的已有节点数 */
  updated: number;
  /** 被 sing-box 判定无法使用、未入库存的节点名（列表页"导入前就已剔除"） */
  skipped?: string[];
  /** 手动重载模式下：这次改动未生效，等手动点"重载" */
  pending_reload?: boolean;
}