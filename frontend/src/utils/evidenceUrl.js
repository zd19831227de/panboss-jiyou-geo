// 证据文件 → 浏览器可打开的相对 URL。
// 后端返回 screenshot_url / raw_artifact_url 时直接使用（统一由后端
// build_evidence_web_url 计算，兼容 Windows 反斜杠与中文文件名）；
// 老数据没有该字段时做兜底转换：反斜杠归一化后截取静态挂载前缀。
export const evidenceUrl = (path, precomputed) => {
  if (precomputed) return precomputed
  if (!path) return ''
  const norm = String(path).replace(/\\/g, '/')
  if (norm.startsWith('/screenshot/') || norm.startsWith('/artifacts/')) return encodeURI(norm)
  const idxShot = norm.indexOf('/screenshot/')
  if (idxShot >= 0) return encodeURI(norm.slice(idxShot))
  const idxArt = norm.indexOf('/artifacts/')
  if (idxArt >= 0) return encodeURI(norm.slice(idxArt))
  return ''
}
