"""Joint reference context, with no fixed-side information."""
import copy
import json

def joint_rank(original_rank, tests, behavior):
    ranked = original_rank(tests, behavior)[:3]
    if not ranked:
        return ranked
    bundle = [{"rank": i, "file": t.file, "name": t.name,
               "code": t.code_content[:40000]} for i, t in enumerate(ranked)]
    # The same primary protocol/placement is used for all independent draws.
    # The model receives the entire reference collection in every draw.
    outputs = []
    for _ in ranked:
        anchor = copy.deepcopy(ranked[0])
        anchor.raw["joint_reference_bundle"] = bundle
        outputs.append(anchor)
    return outputs

def joint_context(test):
    bundle = (test.raw or {}).get("joint_reference_bundle", []) if test else []
    if not bundle:
        return ""
    return ("\n\n[Shared Top-3 reference collection]\n"
            + json.dumps(bundle, ensure_ascii=False)
            + "\nUse all references together as generation context. No reference "
              "is assigned as this candidate's individual adaptation parent. "
              "The primary HostContext only fixes repository-native protocol "
              "and file placement. Construct one target scenario using relevant "
              "evidence from this collection. Generate code directly. "
              "After the first generation, repair the current candidate using "
              "its own execution feedback and this shared reference collection.")
