export type SubtitleFileLike = Pick<File, "name">;

type FileCollection = ArrayLike<File> & {
  item?: (index: number) => File | null;
};

export function isSupportedSubtitleFile(file: SubtitleFileLike): boolean {
  return file.name.trim().toLowerCase().endsWith(".srt");
}

export function getFirstSupportedSubtitleFile(
  files: FileCollection | null | undefined,
): File | null {
  if (!files || files.length === 0) return null;

  for (let index = 0; index < files.length; index += 1) {
    const file = typeof files.item === "function" ? files.item(index) : files[index];
    if (file && isSupportedSubtitleFile(file)) return file;
  }

  return null;
}
