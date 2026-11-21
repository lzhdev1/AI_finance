// 前端唯一的数据入口：所有请求都走 /api，由 nginx 反代到后端容器。
export async function request(url, options = {}) {
  const response = await fetch(import.meta.env.BASE_URL + url.replace(/^\//, ""), {
    headers: { Accept: "application/json" },
    ...options,
  });
  if (!response.ok) {
    const data = await response.json().catch(() => null);
    const error = new Error(
      typeof data?.detail === "string"
        ? data.detail
        : `服务返回 HTTP ${response.status}`,
    );
    error.status = response.status;
    throw error;
  }
  return response.json();
}
