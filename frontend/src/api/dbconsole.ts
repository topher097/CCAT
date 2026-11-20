/** Admin database console helpers. */

import { api } from './client'
import type { Schemas } from './client'

export type Table = Schemas['DbTableView']
export type QueryRequest = Schemas['DbQueryRequest']
export type QueryResult = Schemas['DbQueryResult']

/** GET every table with its columns and row count (admin only). */
export function listTables(): Promise<Table[]> {
  return api.get<Table[]>('/api/v1/admin/db/tables')
}

/** POST one SQL statement (admin only; writes need `write: true`). */
export function runQuery(body: QueryRequest): Promise<QueryResult> {
  return api.post<QueryResult>('/api/v1/admin/db/query', body)
}
