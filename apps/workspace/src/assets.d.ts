declare module "*.png" {
  const path: string;
  export default path;
}

// The part of Vite's import.meta the workspace uses (vite/client is not
// referenced): glob loaders for a cache-busted copy of a lazy chunk.
interface ImportMeta {
  glob(
    pattern: string,
    options?: { query?: string | Record<string, string | number | boolean>; import?: string },
  ): Record<string, () => Promise<unknown>>;
}
