import type { Candidate, CandidateListItem } from "../../lib/api";

export type PatchIndex = {
  candidates?: CandidateListItem[];
  legacyProjectId?: string | null;
  linkableProjects?: { id: string; name: string }[];
};
export type PatchCandidate = Candidate & {
  diff_sha256?: string;
  can_stage?: boolean;
  stage_reason?: string;
  staged?: boolean;
};
