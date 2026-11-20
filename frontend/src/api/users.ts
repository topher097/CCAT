/** Admin user/role helpers (§7.1.11, §8). */

import { api } from './client'
import type { Schemas } from './client'

export type User = Schemas['UserView']
export type UserCreate = Schemas['UserCreate']
export type UserPatch = Schemas['UserPatch']

/** The fixed backend role matrix (D1). */
export const ROLES = ['read_only', 'auditor', 'coordinator', 'admin'] as const

/** GET every staff account (admin only). */
export function listUsers(): Promise<User[]> {
  return api.get<User[]>('/api/v1/users')
}

/** POST one new staff account (admin only). */
export function createUser(body: UserCreate): Promise<User> {
  return api.post<User>('/api/v1/users', body)
}

/** PATCH one account's roles/active/display name (admin only). */
export function patchUser(userId: number, body: UserPatch): Promise<User> {
  return api.patch<User>(`/api/v1/users/${userId}`, body)
}
