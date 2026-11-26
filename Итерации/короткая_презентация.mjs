import fs from 'node:fs/promises';
import path from 'node:path';
const helper='/Users/glebsuchodoev/.codex/plugins/cache/openai-primary-runtime/presentations/26.1020.11127/skills/presentations/container_tools';
const {importRuntimeModule}=await import(helper+'/runtime_helpers.mjs');
const {PresentationFile,FileBlob}=await importRuntimeModule('@oai/artifact-tool');
const {finalizePresentation}=await import(helper+'/artifact_tool_utils.mjs');
const root='/Users/glebsuchodoev/Desktop/workk/ai-sales-assistant';
const target=root+'/Итерации/01_23-24_октября_Основа';
const build=root+'/Итерации/.build/первая_встреча_v3';
await fs.mkdir(build,{recursive:true});
const p=await PresentationFile.importPptx(await FileBlob.load(root+'/Презентация/Защита проекта 2026-10-21.pptx'));
p.slides.keep([0,1,2,7]);
for(const [i,s] of p.slides.items.entries()){
  for(const shape of s.shapes.items){
    if(/^\d{2}$/.test(shape.name||'') && shape.position.left>1100 && shape.position.top>640)
      shape.text=String(i+1).padStart(2,'0');
  }
}
const INK='#151A18', PAPER='#F2F4EF', GRAY='#536158', LIME='#D6FF64';
function text(s,value,x,y,w,h,size=30,color=INK,bold=false){
  const t=s.shapes.add({geometry:'textbox',name:value.slice(0,50),position:{left:x,top:y,width:w,height:h},fill:'none',line:{fill:'none',width:0}});
  t.text=value;t.text.style={fontFamily:'Helvetica Neue',fontSize:size,color,bold};t.text.insets=0;
  return t;
}
let s=p.slides.add({width:1280,height:720,background:{fill:INK}});
text(s,'Следующая проверка:\nзнания из переписки',64,56,1152,145,56,PAPER,true);
text(s,'20 разрешённых пар вопрос–ответ\nКандидат с исходным текстом\nУтверждение и применение факта',64,265,1140,230,40,PAPER,true);
text(s,'К встрече 2 ноября: проверить сценарий с менеджером',68,596,1110,67,29,LIME);
text(s,'05',1160,663,60,30,18,'#BFC9C0');
s.speakerNotes.text='Часть механизма уже прототипирована. Следующий результат команды: проверить сбор и утверждение знаний на 20 разрешённых парах из реальной работы, разобрать ошибки и подтвердить удобство для менеджера. До утверждения новый факт не используется. Здесь просим согласовать приёмку и дату следующей встречи. Источники: Итерации/02_02_ноября_Знания_из_переписки/README.md; app/core/candidates.py.';
s=p.slides.add({width:1280,height:720,background:{fill:PAPER}});
text(s,'Пять зон ответственности',64,56,1152,100,56,INK,true);
text(s,'Предложение для команды, имена ещё согласуем',68,159,1120,50,28,GRAY);
const values=[['У1','Продукт, требования и пилот'],['У2','Материалы бизнеса и разметка'],['У3','Серверная логика и версии знаний'],['У4','RAG, эксперимент и метрики'],['У5','Интерфейс, интеграция и развёртывание']];
const t=s.tables.add({rows:5,columns:2,left:64,top:255,width:1152,height:330,columnWidths:[130,1022],values});
t.cells.block({row:0,column:0,rowCount:5,columnCount:2}).assign({fill:PAPER,textStyle:{fontFamily:'Helvetica Neue',fontSize:31,color:INK},margins:{left:0,right:20,top:12,bottom:12}});
t.cells.block({row:0,column:0,rowCount:5,columnCount:1}).assign({textStyle:{fontFamily:'Helvetica Neue',fontSize:31,color:GRAY,bold:true}});
t.borders.assign({fill:PAPER,width:1,style:'solid'});
for(let i=0;i<5;i++)t.rows[i].height=66;
text(s,'06',1160,663,60,30,18,GRAY);
s.speakerNotes.text='Роли предлагаются для согласования, не подтверждают личное авторство уже готовых модулей. Каждый участник показывает конкретный материал, который подготовил или независимо проверил. Распределение задач и результатов к пяти встречам сохранено в Git (96ce8e0:Итерации/КОМАНДА.md).';
for(let i=0;i<6;i++){
  const image=await p.export({slide:p.slides.items[i],format:'png',scale:1});
  await fs.writeFile(path.join(build,`slide-${i+1}.png`),new Uint8Array(await image.arrayBuffer()));
}
const draft=build+'/candidate.pptx';
await (await PresentationFile.exportPptx(p)).save(draft);
const result=await finalizePresentation({workspaceDir:root,candidatePath:draft,finalPath:target+'/К первой встрече.pptx',
  pythonExecutable:'/Users/glebsuchodoev/.cache/codex-runtimes/codex-primary-runtime/dependencies/python/bin/python3',
  integrityValidatorPath:helper+'/inspect_presentation_package_integrity.py',layoutValidatorPath:helper+'/inspect_presentation_layout_geometry.py',
  layoutArgs:['--expected-slide-size-emu','12192000,6858000','--validate-heading-fit','--require-native-table-slide','6'],
  requiredNativeTableOwnerSlides:[6],fontPolicy:{basis:'design',families:['Helvetica Neue']},verifyArtifactToolImport:true,
  receiptPath:build+'/validation.json'});
console.log(JSON.stringify({slides:p.slides.items.length,finalPath:result.finalPath}));
