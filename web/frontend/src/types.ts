export interface Status {
  listen: string;
  proxy: string;
  nodes: number;
  healthy_nodes: number;
  users: number;
  revoked_users: number;
  sing_box_alive: boolean;
  locked?: boolean;
}

export interface NodeRow {
  tag: string;
  name: string;
  type?: string;
  server?: string;
  port?: number | string;
  healthy: boolean;
  /** 该节点上绑定了几个人（active 用户数） */
  users?: number;
}

/** 轻量节点条目，只用于「换节点」下拉。 */
export interface NodeOption {
  tag: string;
  name: string;
  healthy: boolean;
}

export interface UserRow {
  username: string;
  status: string;
  node_tag: string;
  node_name: string;
  created_at: string;
  revoked_at?: string | null;
  proxy: string;
}

export interface CreatedUser {
  username: string;
  password: string;
  node_tag: string;
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
}
