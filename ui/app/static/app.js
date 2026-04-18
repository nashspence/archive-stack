function appendLine(element, message) {
  if (!element) {
    return;
  }
  element.textContent += `\n${message}`;
  element.scrollTop = element.scrollHeight;
}

function connectProgress(url, output) {
  if (!url || !output || typeof EventSource === "undefined") {
    return null;
  }

  const source = new EventSource(url);
  source.onmessage = (event) => {
    appendLine(output, `progress: ${event.data}`);
  };
  source.onerror = () => {
    appendLine(output, "progress stream disconnected");
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

const MAX_PARALLEL_UPLOADS = 4;
const SKIPPABLE_COLLECTION_UPLOAD_ERROR = "file already uploaded for this collection";

async function runWithConcurrency(items, limit, worker) {
  const queue = Array.from(items);
  let firstError = null;

  async function runner() {
    while (queue.length && !firstError) {
      const item = queue.shift();
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

function wireCollectionUploadForm() {
  const form = document.getElementById("collection-upload-form");
  if (!form) {
    return;
  }

  const plainFilesInput = document.getElementById("collection-files");
  const folderInput = document.getElementById("collection-folder");
  const output = document.getElementById("collection-upload-status");

  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    output.textContent = "Starting uploads.";

    const prefix = form.elements.path_prefix.value;
    const mode = form.elements.mode.value || "0644";
    const uid = form.elements.uid.value;
    const gid = form.elements.gid.value;
    const files = [
      ...Array.from(plainFilesInput.files || []),
      ...Array.from(folderInput.files || []),
    ];

    if (!files.length) {
      output.textContent = "Pick at least one file or folder first.";
      return;
    }

    const progressSource = connectProgress(form.dataset.progressUrl, output);

    try {
      await runWithConcurrency(files, MAX_PARALLEL_UPLOADS, async (file) => {
        const browserRelativePath = file.webkitRelativePath
          ? stripSelectedRoot(file.webkitRelativePath)
          : file.name;
        const relativePath = joinRelativePath(prefix, browserRelativePath);
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

        appendLine(output, `uploading ${relativePath}`);
        const response = await fetch(form.action, { method: "POST", body: payload });
        const body = await parseResponse(response);
        if (!response.ok) {
          if (body.detail === SKIPPABLE_COLLECTION_UPLOAD_ERROR) {
            appendLine(output, `skipped ${relativePath} (already uploaded)`);
            return;
          }
          throw new Error(body.detail || `Upload failed for ${relativePath}`);
        }
        appendLine(output, `completed ${relativePath}`);
      });

      appendLine(output, "All uploads completed. Reloading.");
      window.location.reload();
    } catch (error) {
      appendLine(output, error instanceof Error ? error.message : String(error));
    } finally {
      if (progressSource) {
        progressSource.close();
      }
    }
  });
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

    const progressSource = connectProgress(form.dataset.progressUrl, output);

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
