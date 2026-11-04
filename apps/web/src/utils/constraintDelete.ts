import type { ConstraintItem } from '../api'

/**
 * A5 前端行为边界：
 * - 新增型自然语言 → 只能走后端 /constraints/extract（B 模块 B2），前端不得重写“NL → 新约束对象”。
 * - 删除型自然语言 → 只做“意图分类 + 已有结构化约束匹配定位”，命中唯一后调 DELETE；绝不送 LLM extract。
 * 关键词只认删除动词（删掉塑料约束），"不要塑料"（新增排除约束）不属于删除，仍走 extract。
 */
export const DELETE_TRIGGER = /(删除|删掉|移除|去掉|取消这条|不要再要)/
/** 删除 + 新增混合指令：不做部分执行（静默吞掉“再加 500”是禁区）。 */
export const ADD_TRIGGER = /(再加|加上|加个|加一|新增|增加|补一个|补个|再来)/

export const isActive = (c: ConstraintItem): boolean => c.status !== 'removed'

const TYPE_HINTS: { type: string; re: RegExp }[] = [
  { type: 'budget', re: /预算|budget/i },
  { type: 'functional', re: /功能|人数|人份/i },
  { type: 'taboo', re: /禁忌|禁止|taboo/i },
  { type: 'preference', re: /偏好|喜欢|preference/i },
]

const DELETE_FILLER = /(删除|删掉|移除|去掉|取消这条|不要再要|这条|那条|约束|帮我|请|一下|把|的|了|吧)/g

/** 在现有约束里过滤出删除目标：类型提示 → 文本 token → 数字，逐级收窄。 */
export function matchDeleteTargets(text: string, all: ConstraintItem[]): ConstraintItem[] {
  const active = all.filter(isActive)
  const hint = TYPE_HINTS.find(h => h.re.test(text))
  const candidates = hint ? active.filter(c => c.type === hint.type) : active

  const tokens = text
    .replace(DELETE_FILLER, ' ')
    .split(/[\s,，。;；、]+/)
    .filter(t => t.length >= 2)
  const byContent = candidates.filter(c => {
    const content = (c.content || '').toLowerCase()
    return tokens.some(t => content.includes(t.toLowerCase()))
  })
  if (byContent.length) return byContent

  const nums: string[] = text.match(/\d+(?:\.\d+)?/g) || []
  const byNumber = candidates.filter(c => c.numeric_value != null && nums.includes(String(c.numeric_value)))
  return byNumber.length ? byNumber : candidates
}
