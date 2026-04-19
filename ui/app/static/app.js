function appendLine(element, message) {
  if (!element) {
    return;
  }
  element.textContent += `\n${message}`;
  element.scrollTop = element.scrollHeight;
}

function connectProgress(url, handlers = {}) {
  if (!url || typeof EventSource === "undefined") {
    return null;
  }

  const source = new EventSource(url);
  source.onmessage = (event) => {
    let payload = { raw: event.data };
    try {
      payload = JSON.parse(event.data);
    } catch (_error) {
      // Keep the raw event data when it is not JSON.
    }
    if (handlers.onMessage) {
      handlers.onMessage(payload, event.data);
    }
  };
  source.onerror = () => {
    if (handlers.onError) {
      handlers.onError();
    }
  };
  return source;
}

function stripSelectedRoot(relativePath) {
  if (!relativePath || !relativePath.includes("/")) {
    return relativePath || "";
  }
  return relativePath.split("/").slice(1).join("/");
}

function joinRelativePath(prefix, relativePath) {
  const cleanedPrefix = (prefix || "").trim().replace(/^\/+|\/+$/g, "");
  const cleanedRelative = (relativePath || "").trim().replace(/^\/+/, "");
  if (!cleanedPrefix) {
    return cleanedRelative;
  }
  if (!cleanedRelative) {
    return cleanedPrefix;
  }
  return `${cleanedPrefix}/${cleanedRelative}`;
}

async function parseResponse(response) {
  const contentType = response.headers.get("content-type") || "";
  if (contentType.includes("application/json")) {
    return response.json();
  }
  return { detail: await response.text() };
}

const MAX_PARALLEL_UPLOADS = 8;
const MAX_CONFIGURABLE_PARALLEL_UPLOADS = 16;
const SKIPPABLE_COLLECTION_UPLOAD_ERROR = "file already uploaded for this collection";
const COLLECTION_UPLOAD_PARALLELISM_STORAGE_KEY = "collection-upload-parallelism";

function clampParallelism(value) {
  const parsed = Number.parseInt(String(value), 10);
  if (Number.isNaN(parsed)) {
    return MAX_PARALLEL_UPLOADS;
  }
  return Math.max(1, Math.min(MAX_CONFIGURABLE_PARALLEL_UPLOADS, parsed));
}

function recommendedCollectionUploadParallelism() {
  const hardwareConcurrency = Number(navigator.hardwareConcurrency || 0);
  if (hardwareConcurrency >= 10) {
    return 8;
  }
  if (hardwareConcurrency >= 6) {
    return 6;
  }
  return 4;
}

function loadCollectionUploadParallelism() {
  try {
    const stored = window.localStorage.getItem(COLLECTION_UPLOAD_PARALLELISM_STORAGE_KEY);
    if (stored) {
      return clampParallelism(stored);
    }
  } catch (_error) {
    // Ignore storage failures and use the recommended default.
  }
  return clampParallelism(recommendedCollectionUploadParallelism());
}

function saveCollectionUploadParallelism(value) {
  try {
    window.localStorage.setItem(
      COLLECTION_UPLOAD_PARALLELISM_STORAGE_KEY,
      String(clampParallelism(value))
    );
  } catch (_error) {
    // Ignore storage failures.
  }
}

function formatBytes(bytes) {
  if (!Number.isFinite(bytes) || bytes <= 0) {
    return "0 B";
  }
  const units = ["B", "KiB", "MiB", "GiB", "TiB"];
  let size = bytes;
  let unitIndex = 0;
  while (size >= 1024 && unitIndex < units.length - 1) {
    size /= 1024;
    unitIndex += 1;
  }
  if (unitIndex === 0) {
    return `${Math.round(size)} ${units[unitIndex]}`;
  }
  return `${size.toFixed(size >= 10 ? 0 : 1)} ${units[unitIndex]}`;
}

function formatSpeed(bytesPerSecond) {
  if (!Number.isFinite(bytesPerSecond) || bytesPerSecond <= 0) {
    return "0 B/s";
  }
  return `${formatBytes(bytesPerSecond)}/s`;
}

function formatPercent(value) {
  if (!Number.isFinite(value) || value <= 0) {
    return "0%";
  }
  if (value >= 100) {
    return "100%";
  }
  return `${value >= 10 ? value.toFixed(0) : value.toFixed(1)}%`;
}

async function runWithConcurrency(items, limit, worker) {
  const queue = Array.from(items);
  let nextIndex = 0;
  let firstError = null;

  async function runner() {
    while (!firstError) {
      const item = queue[nextIndex];
      nextIndex += 1;
      if (!item) {
        return;
      }
      try {
        await worker(item);
      } catch (error) {
        firstError = error;
      }
    }
  }

  const workerCount = Math.max(1, Math.min(limit, queue.length || 1));
  await Promise.all(Array.from({ length: workerCount }, () => runner()));
  if (firstError) {
    throw firstError;
  }
}

function setFormBusy(form, busy) {
  for (const element of Array.from(form.elements || [])) {
    if (!(element instanceof HTMLElement)) {
      continue;
    }
    if (busy) {
      element.dataset.wasDisabled = element.disabled ? "true" : "false";
      element.disabled = true;
      continue;
    }
    if (element.dataset.wasDisabled === "false") {
      element.disabled = false;
    }
    delete element.dataset.wasDisabled;
  }
}

function createCollectionUploadProgress({ progressBar, summary, detail, errorBox, totalFiles, totalBytes }) {
  let activeFiles = 0;
  let completedFiles = 0;
  let skippedFiles = 0;
  let completedBytes = 0;
  let skippedBytes = 0;
  let aggregateBaselineBytes = null;
  let aggregateBytes = 0;
  let aggregateSeen = false;
  let lastRenderedBytes = 0;
  let lastRenderedAt = performance.now();
  let bytesPerSecond = 0;

  function displayedBytes() {
    const streamBytes = aggregateSeen ? Math.max(aggregateBytes, completedBytes) + skippedBytes : completedBytes + skippedBytes;
    return Math.min(totalBytes, streamBytes);
  }

  function render(statusMessage) {
    const uploadedBytes = displayedBytes();
    const percent = totalBytes > 0 ? (uploadedBytes / totalBytes) * 100 : 100;
    const now = performance.now();
    if (uploadedBytes > lastRenderedBytes) {
      const elapsedSeconds = Math.max((now - lastRenderedAt) / 1000, 0.001);
      bytesPerSecond = (uploadedBytes - lastRenderedBytes) / elapsedSeconds;
      lastRenderedBytes = uploadedBytes;
      lastRenderedAt = now;
    }

    progressBar.max = Math.max(totalBytes, 1);
    progressBar.value = uploadedBytes;
    summary.textContent = `${formatPercent(percent)} complete · ${formatBytes(uploadedBytes)} / ${formatBytes(totalBytes)} · ${formatSpeed(bytesPerSecond)}`;

    const handledFiles = completedFiles + skippedFiles;
    const parts = [`${handledFiles}/${totalFiles} files handled`];
    if (activeFiles) {
      parts.push(`${activeFiles} active`);
    }
    if (skippedFiles) {
      parts.push(`${skippedFiles} skipped`);
    }
    if (statusMessage) {
      parts.push(statusMessage);
    }
    detail.textContent = parts.join(" · ");
  }

  return {
    start(parallelism) {
      if (errorBox) {
        errorBox.textContent = "";
        errorBox.classList.add("hidden");
      }
      render(`parallelism ${parallelism}`);
    },
    markStarted() {
      activeFiles += 1;
      render();
    },
    markCompleted(sizeBytes) {
      activeFiles = Math.max(0, activeFiles - 1);
      completedFiles += 1;
      completedBytes += sizeBytes;
      render();
    },
    markSkipped(sizeBytes) {
      activeFiles = Math.max(0, activeFiles - 1);
      skippedFiles += 1;
      skippedBytes += sizeBytes;
      render("already present");
    },
    captureAggregate(payload) {
      const currentBytes = Number(payload?.bytes_current);
      if (!Number.isFinite(currentBytes)) {
        return;
      }
      if (aggregateBaselineBytes === null) {
        aggregateBaselineBytes = currentBytes;
      }
      aggregateSeen = true;
      aggregateBytes = Math.max(0, currentBytes - aggregateBaselineBytes);
      render();
    },
    fail(message) {
      activeFiles = 0;
      if (errorBox) {
        errorBox.textContent = message;
        errorBox.classList.remove("hidden");
      }
      render("upload stopped");
    },
    complete() {
      bytesPerSecond = 0;
      progressBar.max = Math.max(totalBytes, 1);
      progressBar.value = progressBar.max;
      summary.textContent = `100% complete · ${formatBytes(totalBytes)} / ${formatBytes(totalBytes)} · done`;
      detail.textContent = `${totalFiles}/${totalFiles} files handled · reloading`;
    },
  };
}

function buildCollectionUploadItems({ plainFiles, folderFiles, prefix, folderMode }) {
  const plainItems = Array.from(plainFiles || []).map((file) => ({
    file,
    relativePath: joinRelativePath(prefix, file.name),
  }));

  const folderItems = Array.from(folderFiles || []).map((file) => {
    const browserRelativePath =
      folderMode === "include-root"
        ? file.webkitRelativePath || file.name
        : stripSelectedRoot(file.webkitRelativePath || file.name);
    return {
      file,
      relativePath: joinRelativePath(prefix, browserRelativePath),
    };
  });

  return [...plainItems, ...folderItems];
}

function wireCollectionUploadForm() {
  const form = document.getElementById("collection-upload-form");
  if (!form) {
    return;
  }

  const plainFilesInput = document.getElementById("collection-files");
  const folderInput = document.getElementById("collection-folder");
  const submitButton = document.getElementById("collection-upload-submit");
  const parallelismInput = document.getElementById("collection-upload-parallelism");
  const folderModeInput = document.getElementById("collection-folder-mode");
  const progressBar = document.getElementById("collection-upload-progress");
  const summary = document.getElementById("collection-upload-summary");
  const detail = document.getElementById("collection-upload-detail");
  const errorBox = document.getElementById("collection-upload-error");

  if (
    !(plainFilesInput instanceof HTMLInputElement) ||
    !(folderInput instanceof HTMLInputElement) ||
    !(submitButton instanceof HTMLButtonElement) ||
    !(parallelismInput instanceof HTMLInputElement) ||
    !(folderModeInput instanceof HTMLSelectElement) ||
    !(progressBar instanceof HTMLProgressElement) ||
    !(summary instanceof HTMLElement) ||
    !(detail instanceof HTMLElement) ||
    !(errorBox instanceof HTMLElement)
  ) {
    return;
  }

  parallelismInput.value = String(loadCollectionUploadParallelism());
  parallelismInput.addEventListener("change", () => {
    const value = clampParallelism(parallelismInput.value);
    parallelismInput.value = String(value);
    saveCollectionUploadParallelism(value);
  });

  let uploadInFlight = false;

  async function handleCollectionUpload(event) {
    event.preventDefault();
    if (uploadInFlight) {
      return;
    }
    uploadInFlight = true;

    const prefix = form.elements.path_prefix.value;
    const mode = form.elements.mode.value || "0644";
    const uid = form.elements.uid.value;
    const gid = form.elements.gid.value;
    const folderMode = folderModeInput.value || "contents-only";
    const parallelism = clampParallelism(parallelismInput.value);
    parallelismInput.value = String(parallelism);
    saveCollectionUploadParallelism(parallelism);
    const items = buildCollectionUploadItems({
      plainFiles: plainFilesInput.files,
      folderFiles: folderInput.files,
      prefix,
      folderMode,
    });

    if (!items.length) {
      errorBox.textContent = "Pick at least one file or folder first.";
      errorBox.classList.remove("hidden");
      uploadInFlight = false;
      return;
    }

    const totalBytes = items.reduce((sum, item) => sum + item.file.size, 0);
    const progress = createCollectionUploadProgress({
      progressBar,
      summary,
      detail,
      errorBox,
      totalFiles: items.length,
      totalBytes,
    });
    progress.start(parallelism);
    setFormBusy(form, true);

    const progressSource = connectProgress(form.dataset.progressUrl, {
      onMessage: (payload) => {
        progress.captureAggregate(payload);
      },
    });

    try {
      await runWithConcurrency(items, parallelism, async ({ file, relativePath }) => {
        progress.markStarted();
        const payload = new FormData();
        payload.append("file", file);
        payload.append("relative_path", relativePath);
        payload.append("size_bytes", String(file.size));
        payload.append("mode", mode);
        payload.append("mtime", new Date(file.lastModified).toISOString());
        if (uid) {
          payload.append("uid", uid);
        }
        if (gid) {
          payload.append("gid", gid);
        }

        const response = await fetch(form.action, { method: "POST", body: payload });
        const body = await parseResponse(response);
        if (!response.ok) {
          if (body.detail === SKIPPABLE_COLLECTION_UPLOAD_ERROR) {
            progress.markSkipped(file.size);
            return;
          }
          throw new Error(body.detail || `Upload failed for ${relativePath}`);
        }
        progress.markCompleted(file.size);
      });

      progress.complete();
      window.location.reload();
    } catch (error) {
      progress.fail(error instanceof Error ? error.message : String(error));
    } finally {
      if (progressSource) {
        progressSource.close();
      }
      setFormBusy(form, false);
      uploadInFlight = false;
    }
  }

  form.addEventListener("submit", handleCollectionUpload);
  submitButton.addEventListener("click", handleCollectionUpload);
}

function wireActivationUploadForm() {
  const form = document.getElementById("activation-upload-form");
  if (!form) {
    return;
  }

  const folderInput = document.getElementById("activation-folder");
  const output = document.getElementById("activation-upload-status");
  const expectedJson = document.getElementById("activation-expected-json");
  const expected = expectedJson ? JSON.parse(expectedJson.textContent) : { entries: [] };

  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    output.textContent = "Starting activation upload.";

    const selectedFiles = Array.from(folderInput.files || []);
    if (!selectedFiles.length) {
      output.textContent = "Pick a restored container root first.";
      return;
    }

    const selectedByPath = new Map();
    for (const file of selectedFiles) {
      const relativePath = stripSelectedRoot(file.webkitRelativePath || file.name);
      selectedByPath.set(relativePath, file);
    }

    const missing = [];
    for (const entry of expected.entries || []) {
      if (!selectedByPath.has(entry.relative_path)) {
        missing.push(entry.relative_path);
      }
    }
    if (missing.length) {
      output.textContent = `Missing ${missing.length} expected files.\n${missing.slice(0, 10).join("\n")}`;
      return;
    }

    const progressSource = connectProgress(form.dataset.progressUrl, {
      onMessage: (_payload, rawData) => {
        appendLine(output, `progress: ${rawData}`);
      },
      onError: () => {
        appendLine(output, "progress stream disconnected");
      },
    });

    try {
      await runWithConcurrency(expected.entries || [], MAX_PARALLEL_UPLOADS, async (entry) => {
        const file = selectedByPath.get(entry.relative_path);
        const payload = new FormData();
        payload.append("file", file);
        payload.append("relative_path", entry.relative_path);

        appendLine(output, `uploading ${entry.relative_path}`);
        const response = await fetch(form.action, { method: "POST", body: payload });
        const body = await parseResponse(response);
        if (!response.ok) {
          throw new Error(body.detail || `Activation upload failed for ${entry.relative_path}`);
        }
        appendLine(output, `completed ${entry.relative_path}`);
      });

      appendLine(output, "Finalizing activation session.");
      const completeResponse = await fetch(form.dataset.completeUrl, { method: "POST" });
      const completeBody = await parseResponse(completeResponse);
      if (!completeResponse.ok) {
        throw new Error(completeBody.detail || "Activation completion failed");
      }

      window.location.assign(completeBody.redirect_url);
    } catch (error) {
      appendLine(output, error instanceof Error ? error.message : String(error));
    } finally {
      if (progressSource) {
        progressSource.close();
      }
    }
  });
}

wireCollectionUploadForm();
wireActivationUploadForm();
