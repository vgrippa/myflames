import type { StateCommand } from "@codemirror/state";
import { selectNextOccurrence, selectSelectionMatches } from "@codemirror/search";

// SQL-aware commands and problem navigation are filled by the feature workers.
export const toggleSqlComment: StateCommand = () => false;
export const selectNextOccurrenceCmd: StateCommand = selectNextOccurrence;
export const selectAllOccurrencesCmd: StateCommand = selectSelectionMatches;
export const showProblemsCmd: StateCommand = () => false;
export const nextProblemCmd: StateCommand = () => false;
export const previousProblemCmd: StateCommand = () => false;
