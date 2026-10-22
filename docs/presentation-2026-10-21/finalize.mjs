import fs from 'node:fs/promises';
import path from 'node:path';
import {finalizePresentation} from '/Users/glebsuchodoev/.codex/plugins/cache/openai-primary-runtime/presentations/26.1020.11127/skills/presentations/container_tools/artifact_tool_utils.mjs';
const root='/Users/glebsuchodoev/Desktop/workk/ai-sales-assistant';
const dir=path.join(root,'docs/presentation-2026-10-21');
const skill='/Users/glebsuchodoev/.codex/plugins/cache/openai-primary-runtime/presentations/26.1020.11127/skills/presentations';
const candidate=path.join(dir,'.build/candidate.pptx');
const exported='/Users/glebsuchodoev/Library/Application Support/Codex/artifact-sessions/exports/latest/6ef21f48a50fd451f0d292bfdafb2b9bcf673ef57f61f61ee4ee11d0d39fb460';
await fs.copyFile(path.join(exported,'defense-final.pptx'),candidate);
const result=await finalizePresentation({workspaceDir:root,candidatePath:candidate,
  finalPath:path.join(root,'Презентация/Защита проекта 2026-10-21.pptx'),
  pythonExecutable:'/Users/glebsuchodoev/.cache/codex-runtimes/codex-primary-runtime/dependencies/python/bin/python3',
  integrityValidatorPath:path.join(skill,'container_tools/inspect_presentation_package_integrity.py'),
  layoutValidatorPath:path.join(skill,'container_tools/inspect_presentation_layout_geometry.py'),
  layoutArgs:['--expected-slide-size-emu','12192000,6858000','--validate-bullet-geometry','--validate-heading-fit',
    '--require-native-table-slide','9','--require-native-table-slide','16'],
  requiredNativeTableOwnerSlides:[9,16],
  fontPolicy:{basis:'design',families:['Helvetica Neue']},verifyArtifactToolImport:true,
  receiptPath:path.join(dir,'.build/validation.json')});
console.log(JSON.stringify(result));
