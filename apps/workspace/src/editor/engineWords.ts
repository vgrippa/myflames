import {
  ENGINE_WORDS_CATALOG,
  type GeneratedSeriesWords,
} from "./engineWords.generated";

export type EngineWord = { name: string; reserved: boolean };

export type EngineWordsResult = {
  release: string | null;
  exact: boolean;
  keywords: EngineWord[];
  grammarFunctions: string[];
};

type DecodedRelease = {
  release: string;
  patch: number;
  keywords: EngineWord[];
  keywordSet: Set<string>;
  grammarFunctions: string[];
};

type DecodedSeries = {
  releases: DecodedRelease[];
  byRelease: Map<string, DecodedRelease>;
};

const VERSION_RE = /(\d+)\.(\d+)\.(\d+)/;
const seriesCache = new Map<string, DecodedSeries>();

function splitWords(raw: string): string[] {
  if (!raw) return [];
  return raw.split(" ").filter(Boolean);
}

function patchOf(release: string): number {
  const parts = release.split(".");
  return parts.length >= 3 ? Number(parts[2]) : -1;
}

function decodeSeries(seriesData: GeneratedSeriesWords): DecodedSeries {
  const kwMap = new Map<string, boolean>();
  for (const word of splitWords(seriesData.baseReserved)) {
    kwMap.set(word, true);
  }
  for (const word of splitWords(seriesData.baseNonReserved)) {
    kwMap.set(word, false);
  }
  const fnSet = new Set<string>(splitWords(seriesData.baseGrammarFunctions));

  const releases: DecodedRelease[] = [];
  const byRelease = new Map<string, DecodedRelease>();

  for (let i = 0; i < seriesData.releases.length; i++) {
    const delta = seriesData.releases[i];
    if (i > 0) {
      if (delta.removed) {
        for (const word of delta.removed) {
          kwMap.delete(word);
        }
      }
      if (delta.added?.reserved) {
        for (const word of delta.added.reserved) {
          kwMap.set(word, true);
        }
      }
      if (delta.added?.nonReserved) {
        for (const word of delta.added.nonReserved) {
          kwMap.set(word, false);
        }
      }
      if (delta.reservedChanged?.toReserved) {
        for (const word of delta.reservedChanged.toReserved) {
          kwMap.set(word, true);
        }
      }
      if (delta.reservedChanged?.toNonReserved) {
        for (const word of delta.reservedChanged.toNonReserved) {
          kwMap.set(word, false);
        }
      }
      if (delta.grammarFunctionsRemoved) {
        for (const fn of delta.grammarFunctionsRemoved) {
          fnSet.delete(fn);
        }
      }
      if (delta.grammarFunctionsAdded) {
        for (const fn of delta.grammarFunctionsAdded) {
          fnSet.add(fn);
        }
      }
    }

    const sortedNames = Array.from(kwMap.keys()).sort();
    const keywords: EngineWord[] = sortedNames.map((name) => ({
      name,
      reserved: Boolean(kwMap.get(name)),
    }));
    const keywordSet = new Set<string>(sortedNames);
    const grammarFunctions = Array.from(fnSet).sort();

    const decoded: DecodedRelease = {
      release: delta.release,
      patch: patchOf(delta.release),
      keywords,
      keywordSet,
      grammarFunctions,
    };
    releases.push(decoded);
    byRelease.set(delta.release, decoded);
  }

  return { releases, byRelease };
}

function getDecodedSeries(
  engine: "mysql" | "mariadb",
  series: string,
): DecodedSeries | null {
  const engineCatalog =
    engine === "mysql" || engine === "mariadb"
      ? ENGINE_WORDS_CATALOG[engine]
      : undefined;
  if (!engineCatalog) return null;
  const seriesData = engineCatalog[series];
  if (!seriesData) return null;

  const cacheKey = `${engine}:${series}`;
  let decoded = seriesCache.get(cacheKey);
  if (!decoded) {
    decoded = decodeSeries(seriesData);
    seriesCache.set(cacheKey, decoded);
  }
  return decoded;
}

function resolveRelease(
  engine: "mysql" | "mariadb",
  version: string,
): { decoded: DecodedRelease; exact: boolean } | null {
  if (typeof version !== "string") return null;
  const trimmed = version.trim();
  if (!trimmed) return null;

  const match = VERSION_RE.exec(trimmed);
  if (!match) return null;

  const major = Number(match[1]);
  const minor = Number(match[2]);
  const patch = Number(match[3]);
  if (!Number.isFinite(major) || !Number.isFinite(minor) || !Number.isFinite(patch)) {
    return null;
  }

  const series = `${major}.${minor}`;
  const decodedSeries = getDecodedSeries(engine, series);
  if (!decodedSeries) return null;

  const exactRelease = decodedSeries.byRelease.get(trimmed);
  if (exactRelease) {
    return { decoded: exactRelease, exact: true };
  }

  let candidate: DecodedRelease | null = null;
  for (const rel of decodedSeries.releases) {
    if (rel.patch <= patch) {
      if (!candidate || rel.patch >= candidate.patch) {
        candidate = rel;
      }
    }
  }
  if (!candidate) return null;
  return { decoded: candidate, exact: false };
}

export function wordsFor(
  engine: "mysql" | "mariadb",
  version: string,
): {
  release: string | null;
  exact: boolean;
  keywords: EngineWord[];
  grammarFunctions: string[];
} {
  const resolved = resolveRelease(engine, version);
  if (!resolved) {
    return {
      release: null,
      exact: false,
      keywords: [],
      grammarFunctions: [],
    };
  }
  return {
    release: resolved.decoded.release,
    exact: resolved.exact,
    keywords: resolved.decoded.keywords,
    grammarFunctions: resolved.decoded.grammarFunctions,
  };
}

export function isKeyword(
  engine: "mysql" | "mariadb",
  version: string,
  name: string,
): boolean {
  if (typeof name !== "string" || !name) return false;
  const resolved = resolveRelease(engine, version);
  if (!resolved) return false;
  return resolved.decoded.keywordSet.has(name.toUpperCase());
}
