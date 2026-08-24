export function applyFlfFields(
  body: Record<string, unknown>,
  imagePath: string | null | undefined,
  endImagePath: string | null | undefined,
  endImageStrength?: number,
): void {
  if (!imagePath || !endImagePath) return
  body.endImagePath = endImagePath
  body.endImageStrength = endImageStrength ?? 0.8
}
