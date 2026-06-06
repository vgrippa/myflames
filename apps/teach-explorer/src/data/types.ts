export type FamilyKey =
  | "scan_family"
  | "index_family"
  | "join_family"
  | "cache_family"
  | "planner_family";

export type CurveKind =
  | "constant"
  | "log"
  | "linear"
  | "nlogn"
  | "quadratic"
  | "linear_sum"
  | "block_nl"
  | "two_log"
  | "sqrt_n"
  | "nested_indexed"
  | "factorial";

export interface Complexity {
  best: string;
  avg: string;
  worst: string;
}

export interface Algorithm {
  name: string;
  family: FamilyKey;
  familyLabel: string;
  familyAccent: string;
  title: string;
  summary: string;
  complexity: Complexity;
  curveKind: CurveKind;
  tags: string[];
  mysqlVersion: string;
  lessonHref: string;
  curriculumStep: number | null;
}

export interface AlgorithmsData {
  lessons: Algorithm[];
  curriculum: string[];
}
