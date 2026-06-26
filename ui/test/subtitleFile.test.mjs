import assert from "node:assert/strict";
import test from "node:test";
import {
  getFirstSupportedSubtitleFile,
  isSupportedSubtitleFile,
} from "../.tmp-tests/subtitleFile.js";

function file(name) {
  return { name };
}

function fileList(files) {
  return {
    length: files.length,
    item(index) {
      return files[index] ?? null;
    },
  };
}

test("recognizes .srt files case-insensitively", () => {
  assert.equal(isSupportedSubtitleFile(file("episode.SRT")), true);
  assert.equal(isSupportedSubtitleFile(file("episode.txt")), false);
});

test("selects the first supported subtitle file from a dropped list", () => {
  const selected = getFirstSupportedSubtitleFile(
    fileList([file("notes.txt"), file("clip.srt"), file("other.srt")]),
  );

  assert.equal(selected.name, "clip.srt");
});

test("returns null when no dropped file is supported", () => {
  assert.equal(
    getFirstSupportedSubtitleFile(fileList([file("notes.txt"), file("video.mp4")])),
    null,
  );
});
