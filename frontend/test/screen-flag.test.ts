/** Live check of the /screen page checkbox against the real component. */

import { describe, expect, it } from 'vitest'
import { flushPromises } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'

import PipelineRunForm from '../src/components/PipelineRunForm.vue'
import { jsonResponse, mockRoutes, mountRouted } from './helpers'

const SETTINGS = {
  command: 'screen',
  run: { overwrite_new: false },
  config_path: '/tmp/ccat.json',
  editable: true,
  saved: false,
}

describe('screen page global flag', () => {
  it('renders the checkbox on the screen page only', async () => {
    setActivePinia(createPinia())
    mockRoutes([
      (url) =>
        url.endsWith('/runs/screen/settings') ? jsonResponse(200, SETTINGS) : undefined,
    ])
    const { wrapper } = await mountRouted(PipelineRunForm, {
      path: '/screen',
      props: { command: 'screen' },
    })
    await flushPromises()
    expect(wrapper.find('input[name="overwrite_new"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="screen-global-settings"]').exists()).toBe(true)
    // The block is labelled as the global setting, and the warning
    // explains what the flag does.
    expect(wrapper.get('[data-testid="screen-global-settings"]').text()).toContain(
      'global',
    )
    expect(wrapper.get('[data-testid="overwrite-new-warning"]').text()).toContain(
      'status is still',
    )
  })

  it('hides the checkbox on the other pipeline pages', async () => {
    setActivePinia(createPinia())
    mockRoutes([])
    const { wrapper } = await mountRouted(PipelineRunForm, {
      path: '/generate',
      props: { command: 'generate' },
    })
    await flushPromises()
    expect(wrapper.find('input[name="overwrite_new"]').exists()).toBe(false)
  })

  it('disables the checkbox for a caller without the editor role', async () => {
    setActivePinia(createPinia())
    mockRoutes([
      (url) =>
        url.endsWith('/runs/screen/settings')
          ? jsonResponse(200, { ...SETTINGS, editable: false })
          : undefined,
    ])
    const { wrapper } = await mountRouted(PipelineRunForm, {
      path: '/screen',
      props: { command: 'screen' },
    })
    await flushPromises()
    const box = wrapper.get('input[name="overwrite_new"]')
    expect((box.element as HTMLInputElement).disabled).toBe(true)
    expect(wrapper.find('[data-testid="read-only"]').exists()).toBe(true)
  })
})
