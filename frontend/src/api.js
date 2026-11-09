async function toError(resp) {
  let detail = `HTTP ${resp.status}`
  try {
    const data = await resp.json()
    if (data.detail) {
      detail = typeof data.detail === 'string' ? data.detail : JSON.stringify(data.detail)
    }
  } catch {
    /* 非 JSON 错误体,保留状态码 */
  }
  return new Error(detail)
}

export async function apiGet(url) {
  const resp = await fetch(url)
  if (!resp.ok) throw await toError(resp)
  return resp.json()
}

export async function apiPost(url, body = {}) {
  const resp = await fetch(url, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  })
  if (!resp.ok) throw await toError(resp)
  return resp.json()
}

export async function apiUpload(url, file) {
  const form = new FormData()
  form.append('file', file)
  const resp = await fetch(url, { method: 'POST', body: form })
  if (!resp.ok) throw await toError(resp)
  return resp.json()
}