import { promises as fs } from "node:fs";
import path from "node:path";

import { del as blobDel, get as blobGet, list as blobList, put as blobPut } from "@vercel/blob";

/**
 * Tiny JSON key/value store. Backed by a (private) Vercel Blob store when
 * BLOB_READ_WRITE_TOKEN is set, otherwise by the local `.state/` directory.
 * This project's store is separate from the dashboard's.
 */
export interface Store {
  getJson<T>(key: string): Promise<T | null>;
  putJson(key: string, value: unknown): Promise<void>;
  /** Keys under `prefix`, newest first, capped at `limit`. */
  listKeys(prefix: string, limit?: number): Promise<string[]>;
  /** Deletes every key under `prefix`; returns how many were removed. */
  deleteByPrefix(prefix: string): Promise<number>;
}

const STATE_DIR = path.resolve(process.cwd(), ".state");

/**
 * Keys must be plain path-safe text. Vercel Blob stores a pathname literally
 * but reads it back through a URL, so a key containing "%" or ":" (e.g. an
 * encodeURIComponent'd id) is written under one name and looked up under
 * another - the read silently returns nothing. Fail loudly instead.
 */
const SAFE_KEY = /^[A-Za-z0-9._\-/]+$/;

function safeKey(key: string): string {
  if (key.includes("..") || key.startsWith("/") || !SAFE_KEY.test(key)) throw new Error(`Invalid store key: ${key}`);
  return key;
}

export function createFileStore(dir = STATE_DIR): Store {
  const fileFor = (key: string) => path.join(dir, safeKey(key));
  return {
    async getJson<T>(key: string) {
      try {
        return JSON.parse(await fs.readFile(fileFor(key), "utf8")) as T;
      } catch (error) {
        if ((error as NodeJS.ErrnoException).code === "ENOENT") return null;
        throw error;
      }
    },
    async putJson(key, value) {
      const file = fileFor(key);
      await fs.mkdir(path.dirname(file), { recursive: true });
      await fs.writeFile(file, JSON.stringify(value, null, 2), "utf8");
    },
    async listKeys(prefix, limit = 1000) {
      const root = path.join(dir, safeKey(prefix));
      const dirName = prefix.endsWith("/") ? root : path.dirname(root);
      const found: Array<{ key: string; mtime: number }> = [];
      async function walk(current: string): Promise<void> {
        let entries: import("node:fs").Dirent[];
        try {
          entries = await fs.readdir(current, { withFileTypes: true });
        } catch {
          return;
        }
        for (const entry of entries) {
          const full = path.join(current, entry.name);
          if (entry.isDirectory()) await walk(full);
          else {
            const key = path.relative(dir, full).split(path.sep).join("/");
            if (key.startsWith(prefix)) found.push({ key, mtime: (await fs.stat(full)).mtimeMs });
          }
        }
      }
      await walk(dirName);
      return found
        .sort((a, b) => b.mtime - a.mtime)
        .slice(0, limit)
        .map((f) => f.key);
    },
    async deleteByPrefix(prefix) {
      const keys = await this.listKeys(prefix, Number.MAX_SAFE_INTEGER);
      await Promise.all(keys.map((key) => fs.rm(fileFor(key), { force: true })));
      return keys.length;
    },
  };
}

export function createBlobStore(): Store {
  return {
    async getJson<T>(key: string) {
      const result = await blobGet(safeKey(key), { access: "private", useCache: false });
      if (!result || result.statusCode !== 200) return null;
      return JSON.parse(await new Response(result.stream).text()) as T;
    },
    async putJson(key, value) {
      await blobPut(safeKey(key), JSON.stringify(value), {
        access: "private",
        addRandomSuffix: false,
        allowOverwrite: true,
        contentType: "application/json",
      });
    },
    async listKeys(prefix, limit = 1000) {
      const blobs: Array<{ pathname: string; uploadedAt: Date }> = [];
      let cursor: string | undefined;
      do {
        const page = await blobList({ prefix, cursor, limit: 1000 });
        blobs.push(...page.blobs.map((b) => ({ pathname: b.pathname, uploadedAt: new Date(b.uploadedAt) })));
        cursor = page.hasMore ? page.cursor : undefined;
      } while (cursor);
      return blobs
        .sort((a, b) => b.uploadedAt.getTime() - a.uploadedAt.getTime())
        .slice(0, limit)
        .map((b) => b.pathname);
    },
    async deleteByPrefix(prefix) {
      const keys = await this.listKeys(prefix, Number.MAX_SAFE_INTEGER);
      for (let i = 0; i < keys.length; i += 100) await blobDel(keys.slice(i, i + 100));
      return keys.length;
    },
  };
}

/**
 * A store for dry runs: reads see the real data (plus anything this run has
 * "written"), but nothing is persisted and nothing is deleted. A dry run
 * therefore never advances a partner's "last email seen" marker or records
 * alerts, so it can be repeated and a later live run still posts everything.
 */
export function createDryRunStore(base: Store): Store {
  const overlay = new Map<string, unknown>();
  return {
    async getJson<T>(key: string) {
      return overlay.has(key) ? (overlay.get(key) as T) : base.getJson<T>(key);
    },
    async putJson(key, value) {
      overlay.set(key, JSON.parse(JSON.stringify(value)));
    },
    listKeys: (prefix, limit) => base.listKeys(prefix, limit),
    async deleteByPrefix() {
      throw new Error("deleteByPrefix is not allowed on a dry-run store");
    },
  };
}

let defaultStore: Store | undefined;

export function getStore(): Store {
  defaultStore ??= process.env.BLOB_READ_WRITE_TOKEN ? createBlobStore() : createFileStore();
  return defaultStore;
}

/** Test hook. */
export function setStoreForTests(store: Store | undefined): void {
  defaultStore = store;
}
