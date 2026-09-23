const base = () => process.env.PERSONZIT_API_URL || 'http://127.0.0.1:8765';
export async function request(route, options = {}) {
  const headers = { 'content-type': 'application/json', ...(options.headers || {}) };
  const token = process.env.PERSONZIT_API_TOKEN;
  if (token) headers.authorization = `Bearer ${token}`;
  let response;
  try { response = await fetch(`${base()}${route}`, { ...options, headers }); }
  catch { throw new Error('PersonZit 服务不可用，请先运行 personzit start'); }
  const body = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(body.detail || `HTTP ${response.status}`);
  return body;
}
