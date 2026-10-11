const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const source = fs.readFileSync(require('node:path').join(__dirname, '../photopilot/server.py'), 'utf8');
const block = (start, end) => source.slice(source.indexOf(start), source.indexOf(end, source.indexOf(start)));

const photo = path => ({path,thumb:'/thumb',width:100,height:100,score:.8,flags:[],rating:0});
const deferred = () => {let resolve,reject;const promise=new Promise((a,b)=>{resolve=a;reject=b;});promise.catch(()=>{});return {promise,resolve,reject};};
async function page(handler=async path=>path==='/api/presets'?{presets:[{name:'natural',polish:{skin:.2}}]}:{state:null},initialFolder='',queueEnabled=false) {
  const nodes=new Map(), listeners={};
  function node(id='') {
    const classes=new Set();
    const n={id,value:'0',min:'0',max:'1',type:'range',checked:false,hidden:false,dataset:{},style:{setProperty(){}},textContent:'',children:[],disabled:false,
      classList:{add(x){classes.add(x);},remove(x){classes.delete(x);},contains(x){return classes.has(x);},toggle(x,on){on??=!classes.has(x);on?classes.add(x):classes.delete(x);}},
      appendChild(c){this.children.push(c);return c;},replaceChildren(){this.children=[];},addEventListener(){},setAttribute(key,value){(this.attributes ||= {})[key]=String(value);},getAttribute(key){return this.attributes?.[key]??null;},dispatchEvent(e){this['on'+e.type]?.(e);},click(){this.onclick?.({target:this});},
      querySelector(s){if(s==='input')return this.input ||= node();if(s==='button.on')return this.buttons.find(b=>b.classList.contains('on'));return (this.queries ||= new Map()).get(s)||((this.queries ||= new Map()).set(s,node()),this.queries.get(s));},querySelectorAll(s){return s==='button'?this.buttons||[]:[];},buttons:[]};
    Object.defineProperty(n,'innerHTML',{get(){return this._html||'';},set(html){this._html=html;this.children=[];const m=html.match(/id="([^"]+)" min="([^"]+)" max="([^"]+)" step="[^"]+" value="([^"]+)"/);if(m){const input=get(m[1]);Object.assign(input,{min:m[2],max:m[3],value:m[4]});this.input=input;}}});
    Object.defineProperty(n,'options',{get(){return this.children.flatMap(c=>c.children?.length?c.children:[c]);}});
    return n;
  }
  function get(id){if(!nodes.has(id))nodes.set(id,node(id));return nodes.get(id);}
  for(const m of source.matchAll(/<input[^>]+id="([^"]+)"[^>]*>/g)){const n=get(m[1]);for(const k of ['type','min','max','value']){const v=m[0].match(new RegExp(k+'="([^"]+)"'));if(v)n[k]=v[1];}n.checked=m[0].includes('checked');}
  const choices={presetSelect:['natural'],algo:['oklab','reinhard','mkl','histogram','luma_hist'],localRegion:['all','skin','face','eyes','background'],exportSize:['2560','original','4096','2048'],exportQuality:['95','90','100'],batchRating:['','1','2','3','4','5','0']};
  choices.similarityProfile=['balanced','strict','loose'];
  for(const [id,values] of Object.entries(choices)){const n=get(id);n.type='select-one';n.value=values[0];n.children=values.map(value=>Object.assign(node(),{value}));}
  get('folder').value='';get('grid').dataset.density='comfortable';
  get('wbSeg').buttons=['batch','single','off'].map((v,i)=>{const b=node();b.dataset.v=v;if(!i)b.classList.add('on');return b;});
  get('viewbar').buttons=['compact','comfortable','large'].map(value=>{const b=node();b.dataset.density=value;return b;});
  get('toneActions').buttons=['reset','save','load'].map(()=>node());
  const images=[];
  let confirmResult=true;const confirmations=[];
  const context=vm.createContext({console,Map,Set,Promise,Date,JSON,Event:class{constructor(type){this.type=type;}},URL:{},Blob:class{},Image:class{constructor(){images.push(this);}},AbortController:class{},
    document:{getElementById:get,querySelectorAll(selector){return selector==='#viewbar button'?get('viewbar').buttons:selector==='.toneActions button'?get('toneActions').buttons:[];},createElement:()=>node(),createDocumentFragment:()=>node(),addEventListener(type,fn){(listeners[type] ||= []).push(fn);},visibilityState:'visible'},
    window:{confirm(message){confirmations.push(message);return confirmResult;},addEventListener(type,fn){(listeners[type] ||= []).push(fn);}},localStorage:{getItem(){return null;},setItem(){}},requestAnimationFrame(){},setTimeout(){return 1;},clearTimeout(){},
    fetch:async(path,opts)=>({ok:true,json:()=>!queueEnabled&&path==='/api/exports'&&!opts?.body?{jobs:[],queue_error:null}:handler(path,opts?.body?JSON.parse(opts.body):null)})});
  const script=source.slice(source.indexOf('<script>')+8,source.indexOf('</script>')).replace('__INIT_FOLDER_JSON__',JSON.stringify(initialFolder));
  vm.runInContext(script,context);
  vm.runInContext('rebuildView=()=>{ranked=filterPhotos(library);};updateTargetUI=()=>{};updateStats=()=>{};toast=()=>{};cardFor=()=>({});flushRings=()=>{};',context);
  await Promise.resolve();await Promise.resolve();
  return {context,nodes,get,listeners,images,confirmations,setConfirm:value=>{confirmResult=value;},run:code=>vm.runInContext(code,context)};
}
const tick=async()=>{for(let i=0;i<8;i++)await Promise.resolve();};
const json=(p,code)=>JSON.parse(p.run('JSON.stringify('+code+')'));
async function editorPage(handler=null){
  const requests=[];let sid=0;
  const p=await page(async(path,body)=>{
    requests.push({path,body});
    if(handler){const result=handler(path,body);if(result!==undefined)return result;}
    return path==='/api/presets'?{presets:[{name:'natural'}]}:path==='/api/project'?{state:null}:path==='/api/prepare'?{sid:'session'+ ++sid,count:body.paths.length,ref_desc:'测试'}:{before:'/original.jpg',after:'/preview.jpg',out:'/out/final.jpg',width:2560,height:1707};
  });await tick();p.run('bindCmp=()=>{};PROJECT.folder="/photos";');
  p.open=async(path='/photos/a.jpg',operation='polish')=>{p.get('folder').value='/photos';p.run('library=['+JSON.stringify(photo(path))+'];ranked=library;selectedPaths=new Set([library[0].path]);');await p.run('process('+JSON.stringify(operation)+')');};
  p.drag=(value,commit=true)=>{const n=p.get('edit-exposure');n.onpointerdown?.({pointerId:1});n.value=String(value);n.oninput();if(commit)n.onpointerup?.({pointerId:1});};
  p.retouch=(key,value,commit=true)=>{const n=p.get('edit-retouch-'+key);n.onpointerdown?.({pointerId:1});n.value=String(value);n.oninput();if(commit)n.onpointerup?.({pointerId:1});};
  p.region=value=>{const n=p.get('editRegion');n.value=value;n.onchange();};
  p.mask=value=>{const n=p.get('editMask');n.checked=value;n.onchange();};
  p.requests=requests;return p;
}
async function checkpoint(p,photos=[photo('/old.jpg')]){return {version:1,folder:'/old',photos,selected:[],target:null,selection_touched:true,settings:JSON.parse(p.run('JSON.stringify(projectSnapshot().settings)')),drafts:{},scan_incomplete:false};}

async function main() {
  let failures = 0;
  const test = async (name, fn) => {
    try { await fn(); console.log('PASS', name); }
    catch (e) { failures++; console.error('FAIL', name, e.message); }
  };
  await test('equal-scoring similar groups remain contiguous', () => {
    const context = vm.createContext({});
    vm.runInContext("let sortMode='smart';" + block('function isGroup', 'function filterPhotos'), context);
    context.photos = [
      {path:'a1',score:.9,group:1,group_size:2,group_rank:1},
      {path:'a2',score:.1,group:1,group_size:2,group_rank:2},
      {path:'b1',score:.9,group:2,group_size:2,group_rank:1},
      {path:'b2',score:.2,group:2,group_size:2,group_rank:2},
    ];
    const sorted = vm.runInContext('sortPhotos(photos)', context);
    assert.deepEqual(Array.from(sorted, p=>p.path), ['a1','a2','b1','b2']);
  });
  await test('grouped view separates each similar set and puts unrelated photos last',async()=>{
    const p=await page();
    p.run(block('function isGroup', 'function rememberView'));
    p.context.photos=[{...photo('/a.jpg'),group:1,group_size:2,group_rank:1},photo('/solo.jpg'),
      {...photo('/b.jpg'),group:2,group_size:2,group_rank:1},
      {...photo('/c.jpg'),group:1,group_size:2,group_rank:2}];
    assert.deepEqual(json(p,'viewGroups(photos).map(g=>g.members.map(p=>p.path))'),[['/a.jpg','/c.jpg'],['/b.jpg'],['/solo.jpg']]);
    assert.equal(p.run('viewGroups(photos)[1].total'),2);
  });
  await test('similar sections retain boundaries across lazy chunks and collapse without recreating cards',async()=>{
    const p=await page();
    p.run(block('function isGroup', 'function rememberView'));
    p.context.photos=Array.from({length:245},(_,i)=>({...photo('/'+i+'.jpg'),group:i<242?1:2,group_size:i<242?242:3,group_rank:i<242?i+1:i-241}));
    p.run('library=photos;library.forEach(p=>libraryByPath.set(p.path,p));cardFor=p=>{let c=cardCache.get(p.path);if(!c){c=document.createElement("div");c.dataset.path=p.path;cardCache.set(p.path,c);}return c;};rebuildView();');
    assert.equal(p.run('groupSections.size'),1);
    assert.equal(p.run('groupSections.get("group:1").body.children.length'),240);
    p.run(block('function renderChunk(', 'const grid='));p.run('renderChunk(false)');
    assert.equal(p.run('groupSections.size'),2);
    assert.equal(p.run('groupSections.get("group:1").body.children.length'),242);
    assert.equal(p.run('groupSections.get("group:2").body.children.length'),3);
    const original=p.run('cardCache.get("/0.jpg")');
    p.run('toggleGroupCollapse("group:1")');assert.equal(p.run('groupSections.get("group:1").body.hidden'),true);
    assert.equal(p.run('groupSections.get("group:1").collapse.getAttribute("aria-expanded")'),'false');
    p.run('rebuildView()');assert.equal(p.run('groupSections.get("group:1").body.hidden'),true);
    assert.equal(p.run('cardCache.get("/0.jpg")'),original);
  });
  await test('group selection affects only visible group members and summaries reflect individual choices',async()=>{
    const p=await page();p.run(block('function isGroup','function rememberView'));
    p.context.photos=[{...photo('/a.jpg'),group:1,group_size:3},{...photo('/b.jpg'),group:1,group_size:3},
      {...photo('/other.jpg'),group:2,group_size:2}];
    p.run('library=[...photos,{...photos[0],path:"/hidden.jpg",flags:["连拍重复"]}];filterMode="recommended";rebuildView();selectViewGroup("group:1");');
    assert.deepEqual(json(p,'Array.from(selectedPaths)'),['/a.jpg','/b.jpg']);
    assert.match(p.run('groupSections.get("group:1").summary.textContent'),/显示 2.*3.*已选 2/);
    p.run('selectedPaths.delete("/a.jpg");updateGroupSummaries();');
    assert.match(p.run('groupSections.get("group:1").summary.textContent'),/已选 1/);
    p.run('selectViewGroup("group:1");selectViewGroup("group:1");');
    assert.equal(p.run('selectedPaths.size'),0);
  });
  await test('score and name modes retain the flat grid unless similar-only filtering is chosen',async()=>{
    const p=await page();p.run(block('function isGroup','function rememberView'));
    p.run('library=['+JSON.stringify({...photo('/a.jpg'),group:1,group_size:2})+'];sortMode="score";rebuildView();');
    assert.equal(p.get('grid').dataset.layout,'flat');
    p.run('filterMode="groups";rebuildView();');assert.equal(p.get('grid').dataset.layout,'groups');
  });
  await test('live scan arrivals respect the grouped layout instead of appending orphan cards',async()=>{
    const p=await page(async path=>path.startsWith('/api/scan_status')?{photos:[{...photo('/a.jpg'),group:1,group_size:2}],total:2,done:1,elapsed:1,finished:false}:{state:null});
    p.run(block('function isGroup','function rememberView'));
    p.run('JOB="scan";poll(POLL_GEN)');await tick();
    assert.equal(p.get('grid').dataset.layout,'groups');assert.equal(p.run('groupSections.size'),1);
  });
  await test('group recommendation label stays on the photo instead of crowding filename badges',()=>{
    const card=block('function makeCard(', 'async function scan(opt)');
    assert.match(card,/\$\{recommend\}\s+<div class="ck">/);
    assert.doesNotMatch(card,/<div class="badges">[^\n]*\$\{recommend\}/);
    assert.match(block('function updateCardInPlace(', 'function mergePhoto('),/card\.querySelector\('\.groupRecommended'\)/);
    assert.match(source,/\.groupRecommended\{position:absolute;top:44px;left:8px/);
  });
  await test('an outdated successful poll cannot clear a newer scan', async () => {
    let resolvePoll, cleared=0;
    const nodes = new Proxy({}, {get:()=>({style:{},classList:{},textContent:''})});
    const context = vm.createContext({JOB:'old', since:0, updatesSince:0, FINAL_STAGE:'', library:[], ranked:[],
      $:id=>nodes[id], api:()=>new Promise(resolve=>{resolvePoll=resolve;}),
      updateStats(){}, scanReset(){cleared++;}, toast(){}, setTimeout(){},
      rebuildView(){}, selectedPaths:new Set(), selectionTouched:true,
    });
    vm.runInContext(block('let POLL_FAIL', 'function cancelScan'), context);
    const pending = vm.runInContext('poll(POLL_GEN)', context);
    vm.runInContext("POLL_GEN++; JOB='new';", context);
    resolvePoll({finished:true,total:0,done:0,refine_pending:false});
    await pending;
    assert.equal(cleared,0);
    assert.equal(context.JOB,'new');
  });
  await test('native folder picker failures are visible and do not silently click the hidden browser fallback', async()=>{
    const p=await page();const messages=[];let fallbackClicks=0;
    p.context.window.pywebview={api:{pick_folder:async()=>{throw Error('native picker failed');}}};
    p.context.toast=message=>messages.push(message);p.get('pickDir').click=()=>fallbackClicks++;
    await p.run('pickFolder()');
    assert.equal(fallbackClicks,0);
    assert.match(p.get('status').textContent,/文件夹选择失败/);
    assert.match(messages.join(' '),/native picker failed/);
  });
  await test('native folder picker selection starts a scan with the selected local path',async()=>{
    const p=await page();let scanPath='',recursive=null;
    p.context.window.pywebview={api:{pick_folder:async()=>'/Users/test/Pictures'}};
    p.context.scan=options=>{scanPath=p.get('folder').value;recursive=options.recursive;};
    p.get('recursiveOpt').checked=true;p.get('folder').value='/Volumes/Offline Drive';
    await p.run('pickFolder()');
    assert.equal(scanPath,'/Users/test/Pictures');assert.equal(recursive,true);
  });
  await test('automatic nested-folder scan reports that child folders were included',async()=>{
    const messages=[];
    const p=await page(async path=>path.startsWith('/api/scan_status')
      ?{job:'nested',finished:true,total:1,done:1,refine_pending:false,recursive_auto:true,
        final:[photo('/local/day-1/photo.jpg')]}
      :path==='/api/scan'?{job:'nested',folder:'/local'}:undefined);
    p.context.toast=message=>messages.push(message);p.get('folder').value='/local';
    await p.run('scan()');await tick();
    assert.match(p.get('scantext').textContent,/已自动搜索子文件夹/);
    assert.match(messages.at(-1),/已自动扫描子文件夹，共读取 1 张照片/);
    assert.equal(p.run('library.length'),1);
  });
  await test('upload preserves exact original File object and extension', async () => {
    const context = vm.createContext({});
    vm.runInContext(block('const RAW_EXT', 'let uploadAbort'), context);
    const file={name:'portrait.png',size:15000000};
    context.file=file;
    const result=await vm.runInContext('prepareBlob(file)',context);
    // Stub createImageBitmap with a decodable large original, as in a browser.
    context.createImageBitmap=async()=>({width:6000,height:4000});
    context.document={createElement(){return {getContext(){return {drawImage(){}};},toBlob(fn){fn({});}};}};
    context.File=function(){return {name:'portrait.jpg'};};
    assert.equal(await vm.runInContext('prepareBlob(file)',context),file);
    assert.equal(result,file);
  });
  await test('a terminal scan failure is visible instead of a success toast', async () => {
    const nodes = Object.create(null);
    const context = vm.createContext({JOB:'job', since:0, updatesSince:0, library:[], ranked:[],
      FINAL_STAGE:'refined', $:id=>(nodes[id] ||= {style:{},classList:{},textContent:''}),
      api:async()=>({finished:true,done:0,total:0,finalize_error:'目录不可读',refine_pending:false}),
      updateStats(){}, scanReset(){}, toast(){}, setTimeout(){}, fmtT(){},finishProjectScan(){},
    });
    vm.runInContext(block('let POLL_FAIL', 'function cancelScan'), context);
    await vm.runInContext('poll(POLL_GEN)',context);
    assert.match(nodes.status.textContent,/目录不可读/);
  });
  await test('late restoration cannot replace a deliberate new scan', async()=>{
    const d=deferred();const p=await page(async path=>path==='/api/project'?d.promise:path==='/api/presets'?{presets:[{name:'natural'}]}:{job:'new',folder:'/new'});
    const state=await checkpoint(p);p.get('folder').value='/new';await p.run('scan()');
    d.resolve({state});await tick();assert.equal(p.run('library.length'),0);assert.equal(p.run('JOB'),'new');
  });
  await test('writes serialize and explicit save acknowledges the latest revision', async()=>{
    const requests=[];const p=await page(async(path,body)=>{if(body){const d=deferred();requests.push({body,d});return d.promise;}return path==='/api/presets'?{presets:[{name:'natural'}]}:{state:null};});await tick();
    p.run("PROJECT.folder='/new';library=["+JSON.stringify(photo('/a.jpg'))+"];projectChanged();");
    const saving=p.run('saveProject()');await tick();p.run('selectedPaths.add("/a.jpg");projectChanged();');
    assert.equal(requests.length,1);requests[0].d.resolve({ok:true,saved_at:1});await tick();assert.equal(requests.length,2);assert.doesNotMatch(p.get('projectStatus').textContent,/已保存/);
    requests[1].d.resolve({ok:true,saved_at:2});assert.equal(await saving,true);assert.deepEqual(requests[1].body.selected,['/a.jpg']);assert.match(p.get('projectStatus').textContent,/已保存/);
  });
  await test('save errors stay visible and the save button retries', async()=>{
    let fail=true;const p=await page(async(path,body)=>{if(body){if(fail)throw Error('磁盘已满');return {ok:true,saved_at:2};}return path==='/api/presets'?{presets:[{name:'natural'}]}:{state:null};});await tick();
    p.run("PROJECT.folder='/new';library=["+JSON.stringify(photo('/a.jpg'))+"];projectChanged();");assert.equal(await p.run('saveProject()'),false);assert.match(p.get('projectStatus').textContent,/磁盘已满/);
    fail=false;assert.equal(await p.run('saveProject()'),true);assert.match(p.get('projectStatus').textContent,/已保存/);
  });
  await test('restore keeps a manually empty selection and waits for preset initialization', async()=>{
    const presets=deferred(),saved=deferred();const p=await page(async path=>path==='/api/presets'?presets.promise:saved.promise);
    const state=await checkpoint(p);state.settings.recipe.sliders.skin=.77;saved.resolve({state});await tick();assert.equal(p.run('library.length'),0);
    presets.resolve({presets:[{name:'natural',polish:{skin:.2}}]});await tick();assert.equal(p.run('library.length'),1);assert.equal(p.run('selectedPaths.size'),0);assert.equal(+p.get('skin').value,.77);assert.equal(p.run('selectionTouched'),true);
  });
  await test('missing records block saves until a successful scan supplies usable photos', async()=>{
    const saved=deferred();let writes=0;const p=await page(async(path,body)=>{if(body&&path==='/api/project'){writes++;return {ok:true};}return path==='/api/presets'?{presets:[{name:'natural'}]}:path==='/api/project'?saved.promise:{job:'new',folder:'/new'};});
    const state=await checkpoint(p,[photo('/good.jpg'),photo('/<missing>.jpg')]);saved.resolve({state,missing:['/<missing>.jpg'],changed:[]});await tick();assert.equal(p.run('library.length'),1);assert.equal(await p.run('saveProject()'),false);assert.equal(writes,0);assert.match(p.get('projectStatus').textContent,/<missing>/);
    p.get('folder').value='/new';await p.run('scan()');assert.equal(p.run('PROJECT.blocked'),true);p.run('finishProjectScan(false)');assert.equal(p.run('PROJECT.blocked'),true);assert.equal(p.run('library[0].path'),'/good.jpg');
    await p.run('scan()');p.run('acceptProjectScan('+JSON.stringify([photo('/new.jpg')])+');finishProjectScan(true);');assert.equal(p.run('PROJECT.blocked'),false);assert.equal(p.run('PROJECT.folder'),'/new');
  });
  await test('invalid saved settings fail atomically and preserve the checkpoint', async()=>{
    const saved=deferred();const p=await page(async path=>path==='/api/presets'?{presets:[{name:'natural'}]}:saved.promise);const state=await checkpoint(p);state.settings.recipe.sliders.skin=7;saved.resolve({state});await tick();assert.equal(p.run('library.length'),0);assert.equal(p.run('PROJECT.blocked'),true);assert.notEqual(+p.get('skin').value,7);
  });
  await test('blocked recovery stays protected through partial failed and cancelled scans', async()=>{
    const saved=deferred();const p=await page(async path=>path==='/api/presets'?{presets:[{name:'natural'}]}:saved.promise);const state=await checkpoint(p);
    saved.resolve({state,changed:['/old.jpg']});await tick();
    p.run('beginProjectScan("/new");acceptProjectScan('+JSON.stringify([photo('/new.jpg')])+');');
    assert.equal(p.run('PROJECT.blocked'),true);p.run('finishProjectScan(false)');assert.equal(p.run('PROJECT.blocked'),true);assert.equal(p.run('PROJECT.scanIncomplete'),true);
    p.run('beginProjectScan("/new");acceptProjectScan('+JSON.stringify([photo('/new.jpg')])+');finishProjectScan(true);');assert.equal(p.run('PROJECT.blocked'),false);assert.equal(p.run('PROJECT.scanIncomplete'),false);
  });
  await test('real poll preserves old photos on empty terminal scan and saves only usable canonical imports', async()=>{
    let pollReply={finished:true,total:0,done:0,refine_pending:false,final:[]};const writes=[],messages=[];
    const p=await page(async(path,body)=>path==='/api/presets'?{presets:[{name:'natural'}]}:path==='/api/project'?body?(writes.push(body),{ok:true}):{state:null}:path.startsWith('/api/scan_status')?pollReply:{job:'new',folder:'/canonical/new',files:['/canonical/new/a.jpg'],count:1});await tick();
    p.context.toast=message=>messages.push(message);
    p.run('PROJECT.folder="/old";library=['+JSON.stringify(photo('/old.jpg'))+'];libraryByPath.set(library[0].path,library[0]);projectChanged();');
    p.get('folder').value='~/empty';await p.run('scan()');await tick();assert.equal(p.run('library[0].path'),'/old.jpg');assert.equal(p.run('PROJECT.folder'),'/old');
    assert.match(p.get('scantext').textContent,/0 张.*原有 .*张已保留/);assert.match(messages.at(-1),/没有可读取的照片.*原有 1 张照片已保留/);
    pollReply={finished:true,total:1,done:1,refine_pending:false,final:[photo('/canonical/new/a.jpg')]};await p.run('importLocalPaths(["~/a.jpg"])');await tick();assert.equal(p.run('PROJECT.folder'),'/canonical/new');assert.equal(p.run('library[0].path'),'/canonical/new/a.jpg');
    await p.run('saveProject()');assert.equal(writes.at(-1).folder,'/canonical/new');assert.equal(writes.at(-1).scan_incomplete,false);assert.equal('JOB' in writes.at(-1),false);
  });
  await test('pending manual XMP rating is not checkpointed before successful write', async()=>{
    const rating=deferred();const p=await page(async(path,body)=>path==='/api/presets'?{presets:[{name:'natural'}]}:path==='/api/rate'?rating.promise:{state:null});await tick();
    p.run('PROJECT.folder="/old";library=['+JSON.stringify(photo('/old.jpg'))+'];syncRatingUI=()=>{};refreshRatedView=()=>{};');
    const pending=p.run('toggleStar(library[0])');assert.equal(p.run('projectSnapshot().photos[0].rating'),0);rating.resolve({ok:true});await pending;assert.equal(p.run('projectSnapshot().photos[0].rating'),5);
  });
  for(const success of [true,false])await test('replacement snapshot ignores old same-path pending rating before and after '+(success?'success':'failure'),async()=>{
    const ack=deferred();const p=await page(async path=>path==='/api/rate'?ack.promise:{state:null});await tick();
    p.run('replaceProjectLibrary(['+JSON.stringify(photo('/a.jpg'))+']);');const work=p.run('setRating(library[0],3)');await tick();
    assert.equal(p.run('projectSnapshot().photos[0].rating'),0);
    p.run('replaceProjectLibrary(['+JSON.stringify({...photo('/a.jpg'),rating:5})+']);');
    assert.equal(p.run('pendingRatings.has("/a.jpg")'),true);assert.equal(await p.run('setRating(library[0],4)'),false);
    assert.equal(p.run('projectSnapshot().photos[0].rating'),5);
    if(success)ack.resolve({ok:true});else ack.reject(Error('disk unavailable'));await work;
    assert.equal(p.run('projectSnapshot().photos[0].rating'),5);assert.equal(p.run('pendingRatings.size'),0);
  });
  await test('a selected card can receive an exact 1–5 XMP rating',async()=>{
    const writes=[];const p=await page(async(path,body)=>path==='/api/rate'?(writes.push(body),{ok:true}):{state:null});await tick();
    p.run('PROJECT.folder="/photos";library=['+JSON.stringify(photo('/photos/a.jpg'))+'];libraryByPath.set(library[0].path,library[0]);');
    assert.equal(await p.run('setRating(library[0],3)'),true);assert.equal(p.run('library[0].rating'),3);assert.equal(writes.length,1);assert.equal(writes[0].rating,3);
  });
  await test('photo cards expose explicit 1–5 choices and a separate clear action',()=>{
    const cardCode=block('function makeCard(', 'async function scan(opt)');
    for(let rating=1;rating<=5;rating++)assert.match(cardCode,new RegExp(`data-rating="${rating}"`));
    assert.match(cardCode,/data-rating="0"/);assert.match(cardCode,/toggleRatingMenu\(p,d\)/);assert.match(cardCode,/chooseCardRating\(p,Number\(choice\.dataset\.rating\),d\)/);
  });
  await test('selection toolbar separates project removal from recoverable local trash',()=>{
    assert.match(source,/id="projectRemoveBtn"[^>]*onclick="removeSelectedFromProject\(\)"/);
    assert.match(source,/id="trashBtn"[^>]*onclick="deleteSelectedToTrash\(\)"/);
    assert.match(source,/删除原片可从 macOS 废纸篓恢复/);
  });
  await test('selection counters explicitly distinguish visible selected from all selected',async()=>{
    const p=await page();p.run(block('function updateStats()', 'function updateTargetUI()'));
    p.run('library='+JSON.stringify([photo('/visible.jpg'),photo('/hidden.jpg'),photo('/unselected.jpg')])+';ranked=[library[0]];selectedPaths=new Set(["/visible.jpg","/hidden.jpg","/missing.jpg"]);updateStats();');
    assert.match(p.get('scanInfo').textContent,/当前可见已选 1/);
    assert.match(p.get('scanInfo').textContent,/全部已选 2/);
    assert.match(p.get('batchSelectionCount').textContent,/全部已选 2.*当前可见 1.*隐藏 1/);
    assert.equal(p.get('clearHiddenSelection').hidden,false);
  });
  await test('clearing filtered-out selection preserves visible choices and never writes photos',async()=>{
    const writes=[];const p=await page(async(path,body)=>{if(body)writes.push(path);return {state:null};});
    p.run('library='+JSON.stringify([photo('/visible.jpg'),photo('/hidden.jpg')])+';ranked=[library[0]];selectedPaths=new Set(library.map(p=>p.path));clearHiddenSelection();');
    assert.deepEqual(json(p,'Array.from(selectedPaths)'),['/visible.jpg']);assert.equal(p.run('selectionTouched'),true);
    assert.equal(p.get('clearHiddenSelection').hidden,true);assert.deepEqual(writes,[]);
  });
  for(const action of ['removeSelectedFromProject','deleteSelectedToTrash'])await test(action+' warns exactly how many filtered-out photos it includes',async()=>{
    const p=await page();await tick();p.setConfirm(false);
    p.run('PROJECT.ready=true;PROJECT.blocked=false;library='+JSON.stringify([photo('/visible.jpg'),photo('/hidden.jpg')])+';ranked=[library[0]];selectedPaths=new Set(library.map(p=>p.path));');
    assert.equal(await p.run(action+'()'),0);
    assert.match(p.confirmations[0],/当前可见 1 张.*隐藏 1 张/);
    assert.match(p.confirmations[0],/隐藏照片也包含在本次操作中/);
    assert.equal(p.run('library.length'),2);
  });
  await test('batch rating requires confirmation before touching hidden selected photos',async()=>{
    const writes=[];const p=await page(async(path,body)=>path==='/api/rate'?(writes.push(body),{ok:true}):{state:null});p.setConfirm(false);
    p.run('library='+JSON.stringify([photo('/visible.jpg'),photo('/hidden.jpg')])+';ranked=[library[0]];selectedPaths=new Set(library.map(p=>p.path));');
    assert.equal(await p.run('applyBatchRating(3)'),0);assert.match(p.confirmations[0],/隐藏 1 张/);assert.deepEqual(writes,[]);
  });
  await test('group navigation crosses lazy chunk boundary and does not wrap past the ends',async()=>{
    const p=await page();p.run(block('function isGroup','function rememberView'));
    p.context.photos=Array.from({length:245},(_,i)=>({...photo('/'+i+'.jpg'),group:i<242?1:2,group_size:i<242?242:3}));
    p.run('library=photos;rebuildView();');
    assert.equal(p.run('navigateGroup(-1)'),false);
    assert.equal(p.run('navigateGroup(1)'),true);
    assert.equal(p.run('activeGroupKey'),'group:2');assert.equal(p.run('groupSections.has("group:2")'),true);
    assert.equal(p.run('navigateGroup(1)'),false);
    assert.equal(p.run('navigateGroup(-1)'),true);assert.equal(p.run('activeGroupKey'),'group:1');
  });
  await test('folding all groups preserves choices and reused card nodes',async()=>{
    const p=await page();p.run(block('function isGroup','function rememberView'));
    p.run('library='+JSON.stringify([{...photo('/a.jpg'),group:1,group_size:2},{...photo('/b.jpg'),group:2,group_size:2}])+';rebuildView();selectedPaths.add("/a.jpg");setAllGroupsCollapsed(true);');
    assert.equal(p.run('collapsedGroups.size'),2);assert.equal(p.run('selectedPaths.has("/a.jpg")'),true);
    p.run('setAllGroupsCollapsed(false)');assert.equal(p.run('collapsedGroups.size'),0);
  });
  await test('main grid number keys rate selected photos and reject repeats editing and modifiers',async()=>{
    const p=await page();const ratings=[];p.context.applyBatchRating=r=>ratings.push(r);
    const event={key:'3',target:{tagName:'DIV'},preventDefault(){this.prevented=true;}};
    assert.equal(p.run('gridRatingShortcut')(event),true);assert.deepEqual(ratings,[3]);
    for(const changes of [{repeat:true},{isComposing:true},{metaKey:true},{ctrlKey:true},{altKey:true},{shiftKey:true},
      {target:{tagName:'INPUT'}},{target:{tagName:'TEXTAREA'}},{target:{tagName:'SELECT'}},{target:{isContentEditable:true}}])
      assert.equal(p.run('gridRatingShortcut')({...event,...changes}),false);
    p.get('results').classList.add('show');assert.equal(p.run('gridRatingShortcut')(event),false);
    assert.deepEqual(ratings,[3]);
  });
  await test('culling undo and redo restore explicit selection without changing ratings',async()=>{
    const p=await page();await tick();p.run('library='+JSON.stringify([photo('/a.jpg'),photo('/b.jpg')])+';library.forEach(p=>libraryByPath.set(p.path,p));ranked=library;');
    p.run('selAll(1)');assert.equal(p.run('selectedPaths.size'),2);
    assert.equal(await p.run('undoCulling()'),true,p.run('JSON.stringify({history:CULL_HISTORY.undo,blocked:PROJECT.blocked,scan:SCAN_ACTIVE,job:JOB,process:PROCESS_BUSY,editor:!!CURRENT_EDITOR})'));assert.equal(p.run('selectedPaths.size'),0);
    assert.equal(await p.run('redoCulling()'),true);assert.equal(p.run('selectedPaths.size'),2);
    assert.equal(p.run('library[0].rating'),0);
  });
  await test('successful batch ratings undo as one action and redo writes exact acknowledged values',async()=>{
    const writes=[];const p=await page(async(path,body)=>path==='/api/rate'?(writes.push(body),{ok:true}):{state:null});await tick();
    p.run('library='+JSON.stringify([photo('/a.jpg'),{...photo('/b.jpg'),rating:2}])+';library.forEach(p=>libraryByPath.set(p.path,p));ranked=library;selectedPaths=new Set(library.map(p=>p.path));');
    await p.run('applyBatchRating(4)');assert.equal(p.run('CULL_HISTORY.undo.length'),1);
    assert.equal(await p.run('undoCulling()'),true);assert.deepEqual(json(p,'library.map(p=>p.rating)'),[0,2]);
    assert.equal(await p.run('redoCulling()'),true);assert.deepEqual(json(p,'library.map(p=>p.rating)'),[4,4]);
  });
  await test('failed rating undo remains retryable and library replacement clears history',async()=>{
    let fail=false;const p=await page(async(path)=>path==='/api/rate'&&fail?{error:'disk disconnected'}:{state:null});await tick();
    p.run('library=['+JSON.stringify(photo('/a.jpg'))+'];libraryByPath.set(library[0].path,library[0]);ranked=library;');
    await p.run('setRating(library[0],3)');fail=true;
    assert.equal(await p.run('undoCulling()'),false);assert.equal(p.run('library[0].rating'),3);assert.equal(p.run('CULL_HISTORY.undo.length'),1);
    fail=false;assert.equal(await p.run('undoCulling()'),true);assert.equal(p.run('library[0].rating'),0);
    p.run('replaceProjectLibrary([])');assert.equal(p.run('CULL_HISTORY.redo.length'),0);
  });
  await test('rating undo stops unsent writes when the library is replaced with identical paths',async()=>{
    const ack=deferred(),writes=[];let defer=false;
    const p=await page(async(path,body)=>path==='/api/rate'?(writes.push(body),defer?ack.promise:{ok:true}):{state:null});await tick();
    const photos=[photo('/a.jpg'),photo('/b.jpg')];p.run('replaceProjectLibrary('+JSON.stringify(photos)+');ranked=library;selectedPaths=new Set(library.map(p=>p.path));');
    await p.run('applyBatchRating(4)');defer=true;const work=p.run('undoCulling()');await tick();assert.equal(writes.length,3);
    p.run('replaceProjectLibrary('+JSON.stringify(photos.map(p=>({...p,rating:5})))+')');const intent=p.run('PROJECT.intent');ack.resolve({ok:true});
    assert.equal(await work,false);assert.deepEqual(json(p,'library.map(p=>p.rating)'),[5,5]);assert.equal(writes.length,3);
    assert.equal(p.run('PROJECT.intent'),intent);assert.equal(p.run('CULL_HISTORY.undo.length'),0);assert.equal(p.run('CULL_HISTORY.redo.length'),0);
  });
  for(const redo of [false,true])for(const saved of [false,true])await test('removal '+(redo?'redo':'undo')+' ignores '+(saved?'successful':'failed')+' save after replacement',async()=>{
    const p=await page();await tick();p.run('replaceProjectLibrary(['+JSON.stringify(photo('/a.jpg'))+']);ranked=library;selectedPaths.add("/a.jpg");saveProject=async()=>true;');
    await p.run('removeSelectedFromProject()');if(redo)await p.run('undoCulling()');
    const ack=deferred();p.context.saveAck=ack.promise;p.run('saveProject=()=>saveAck');const work=p.run(redo?'redoCulling()':'undoCulling()');await tick();
    p.run('replaceProjectLibrary(['+JSON.stringify({...photo('/a.jpg'),rating:5})+'])');const before=json(p,'library'),intent=p.run('PROJECT.intent');ack.resolve(saved);
    assert.equal(await work,false);assert.deepEqual(json(p,'library'),before);assert.equal(p.run('PROJECT.intent'),intent);assert.equal(p.run('CULL_HISTORY.undo.length'),0);assert.equal(p.run('CULL_HISTORY.redo.length'),0);
  });
  await test('reused photo cards synchronize target styling before remount',async()=>{
    const p=await page();await tick();p.run(block('function updateCardInPlace(', 'function mergePhoto('));
    const card=p.get('cachedCard'),button=card.querySelector('.targetBtn');card.classList.add('target');button.classList.add('on');button.textContent='目标照片';button.setAttribute('aria-pressed','true');
    card.querySelector=selector=>selector==='.targetBtn'?button:null;p.context.cachedCard=card;p.context.cachedPhoto=photo('/a.jpg');
    p.run('targetPath="/b.jpg";updateCardInPlace(cachedCard,cachedPhoto)');assert.equal(card.classList.contains('target'),false);assert.equal(button.classList.contains('on'),false);assert.equal(button.textContent,'设为目标');assert.equal(button.getAttribute('aria-pressed'),'false');
    p.run('targetPath="/a.jpg";updateCardInPlace(cachedCard,cachedPhoto)');assert.equal(card.classList.contains('target'),true);assert.equal(button.getAttribute('aria-pressed'),'true');
  });
  await test('virtual group navigation recognizes the short final group at the scroll bottom',async()=>{
    const p=await page();await tick();p.run(block('function syncGroupNavigation(', 'function navigateGroup('));
    Object.assign(p.get('grid'),{scrollTop:900,clientHeight:100,scrollHeight:1000,getBoundingClientRect:()=>({top:0})});
    p.run('groupedView=()=>true;virtualGroupedView=()=>true;VIEW_GROUPS=[{key:"first",group:1,title:"First"},{key:"last",group:2,title:"Last"}];activeGroupKey="first";groupJumpTop=null;renderedN=12;ranked=new Array(6000);groupSections.set("first",{section:{getBoundingClientRect:()=>({top:-500})}});groupSections.set("last",{section:{getBoundingClientRect:()=>({top:50})}});syncGroupNavigation(true)');
    assert.equal(p.run('activeGroupKey'),'last');assert.equal(p.get('nextGroup').disabled,true);
  });
  await test('project removal undo restores photos drafts and selection without local deletion',async()=>{
    const writes=[];const p=await page(async(path,body)=>{if(body)writes.push(path);return path==='/api/project'&&body?{ok:true,saved_at:1}:{state:null};});await tick();
    p.run('PROJECT.ready=true;PROJECT.folder="/photos";library=['+JSON.stringify(photo('/photos/a.jpg'))+'];libraryByPath.set(library[0].path,library[0]);ranked=library;selectedPaths.add(library[0].path);EDIT_DRAFTS.set(library[0].path,{draft:1});');
    await p.run('removeSelectedFromProject()');assert.equal(p.run('library.length'),0);
    assert.equal(await p.run('undoCulling()'),true);assert.equal(p.run('library.length'),1);assert.equal(p.run('selectedPaths.size'),1);assert.equal(p.run('EDIT_DRAFTS.size'),1);
    assert.equal(await p.run('redoCulling()'),true);assert.equal(p.run('library.length'),0);
    assert.ok(writes.every(path=>path==='/api/project'));
  });
  await test('manual group split and merge preserve ratings selections and support undo',async()=>{
    const p=await page();await tick();p.run('library='+JSON.stringify([{...photo('/a.jpg'),group:1,group_size:3,rating:4},{...photo('/b.jpg'),group:1,group_size:3},{...photo('/c.jpg'),group:1,group_size:3},{...photo('/other.jpg'),group:2,group_size:1}])+';library.forEach(p=>libraryByPath.set(p.path,p));ranked=library;selectedPaths=new Set(["/a.jpg","/b.jpg"]);');
    p.run('editSimilarGroups("split")');assert.deepEqual(json(p,'library.slice(0,3).map(p=>p.group_size)'),[1,1,1]);
    assert.equal(p.run('library[0].rating'),4);assert.equal(p.run('selectedPaths.size'),2);
    assert.equal(await p.run('undoCulling()'),true);assert.deepEqual(json(p,'library.slice(0,3).map(p=>p.group_size)'),[3,3,3]);
    p.run('editSimilarGroups("merge")');assert.deepEqual(json(p,'library.slice(0,3).map(p=>p.group_size)'),[2,2,1]);
    assert.equal(p.run('library[3].group'),2);assert.equal(p.run('library[0].rating'),4);
  });
  await test('manual grouping leaves unrelated unavailable and singleton metadata exactly unchanged',async()=>{
    const p=await page();await tick();const photos=[{...photo('/a.jpg'),group:1,group_size:2},{...photo('/b.jpg'),group:1,group_size:2},{...photo('/missing.jpg'),group:-1,group_size:1,group_rank:0,group_best:false,flags:['读取失败','连拍重复']},{...photo('/other.jpg'),group:9,group_rank:0,group_best:false,flags:['自定义']}];
    p.run('library='+JSON.stringify(photos)+';library.forEach(p=>libraryByPath.set(p.path,p));ranked=library;selectedPaths=new Set(["/a.jpg"]);');const before=json(p,'library.slice(2)');
    p.run('editSimilarGroups("split")');assert.deepEqual(json(p,'library.slice(2)'),before);assert.ok(json(p,'CULL_HISTORY.undo.at(-1).changes').every(change=>!['/missing.jpg','/other.jpg'].includes(change.path)));
  });
  for(const action of ['removeSelectedFromProject','deleteSelectedToTrash'])await test(action+' blocks selected photos with pending XMP writes',async()=>{
    const ack=deferred(),writes=[];const p=await page(async(path,body)=>{if(body)writes.push(path);return path==='/api/rate'?ack.promise:path==='/api/project'&&body?{ok:true,saved_at:1}:{state:null};});await tick();
    p.run('PROJECT.ready=true;PROJECT.folder="/photos";library=['+JSON.stringify(photo('/photos/a.jpg'))+'];libraryByPath.set(library[0].path,library[0]);ranked=library;selectedPaths.add(library[0].path);');const rating=p.run('setRating(library[0],3)');await tick();
    assert.equal(await p.run(action+'()'),0);assert.equal(p.run('library.length'),1);assert.equal(p.confirmations.length,0);assert.deepEqual(writes,['/api/rate']);ack.resolve({ok:true});await rating;assert.equal(p.run('CULL_HISTORY.undo.length'),1);assert.equal(await p.run('undoCulling()'),true);
  });
  for(const batch of [false,true])await test('late '+(batch?'batch':'single')+' rating acknowledgement cannot alter a replacement library or history',async()=>{
    const ack=deferred();const p=await page(async path=>path==='/api/rate'?ack.promise:{state:null});await tick();p.run('library=['+JSON.stringify(photo('/old.jpg'))+'];libraryByPath.set(library[0].path,library[0]);ranked=library;selectedPaths.add(library[0].path);');
    const work=p.run(batch?'applyBatchRating(4)':'setRating(library[0],4)');await tick();p.run('replaceProjectLibrary(['+JSON.stringify(photo('/old.jpg'))+'])');const before=json(p,'library'),intent=p.run('PROJECT.intent');ack.resolve({ok:true});await work;
    assert.deepEqual(json(p,'library'),before);assert.equal(p.run('PROJECT.intent'),intent);assert.equal(p.run('CULL_HISTORY.undo.length'),0);assert.equal(p.run('CULL_HISTORY.redo.length'),0);assert.equal(p.run('pendingRatings.size'),0);
  });
  await test('replacing a library stops unsent batch ratings after in-flight writes acknowledge',async()=>{
    const ack=deferred(),writes=[];const p=await page(async(path,body)=>path==='/api/rate'?(writes.push(body),ack.promise):{state:null});await tick();const photos=Array.from({length:12},(_,i)=>photo('/old'+i+'.jpg'));
    p.run('library='+JSON.stringify(photos)+';library.forEach(p=>libraryByPath.set(p.path,p));ranked=library;selectedPaths=new Set(library.map(p=>p.path));');const work=p.run('applyBatchRating(2)');await tick();assert.equal(writes.length,8);
    p.run('replaceProjectLibrary([])');ack.resolve({ok:true});await work;assert.equal(writes.length,8);assert.equal(p.run('CULL_HISTORY.undo.length'),0);assert.equal(p.run('BATCH_BUSY'),false);assert.equal(p.run('pendingRatings.size'),0);
  });
  await test('failed old rating write cannot rollback a replacement photo or change project intent',async()=>{
    const ack=deferred();const p=await page(async path=>path==='/api/rate'?ack.promise:{state:null});await tick();p.run('library=['+JSON.stringify(photo('/same.jpg'))+'];libraryByPath.set(library[0].path,library[0]);');const work=p.run('setRating(library[0],5)');await tick();p.run('replaceProjectLibrary(['+JSON.stringify({...photo('/same.jpg'),rating:3})+'])');const intent=p.run('PROJECT.intent');ack.reject(Error('disk unavailable'));assert.equal(await work,false);assert.equal(p.run('library[0].rating'),3);assert.equal(p.run('PROJECT.intent'),intent);assert.equal(p.run('CULL_HISTORY.undo.length'),0);
  });
  await test('old project settings accept missing similarity profile and reject unknown values',async()=>{
    const p=await page();await tick();
    const settings=json(p,'projectSnapshot().settings');delete settings.similarityProfile;
    p.context.settings=settings;assert.doesNotThrow(()=>p.run('validateProjectSettings(settings)'));
    settings.similarityProfile='unknown';assert.throws(()=>p.run('validateProjectSettings(settings)'),/相似|similarity/);
  });
  await test('lightbox acknowledged ratings participate in culling undo',async()=>{
    const p=await page();await tick();p.run('library=['+JSON.stringify(photo('/a.jpg'))+'];libraryByPath.set(library[0].path,library[0]);ranked=library;LBI=0;');
    await p.run('lbRate(2)');assert.equal(await p.run('undoCulling()'),true);assert.equal(p.run('library[0].rating'),0);
  });
  await test('batch rating applies the chosen value only to selected photos',async()=>{
    const writes=[];const p=await page(async(path,body)=>path==='/api/rate'?(writes.push(body),{ok:true}):{state:null});await tick();
    p.run('PROJECT.folder="/photos";library='+JSON.stringify([photo('/photos/a.jpg'),photo('/photos/b.jpg'),photo('/photos/c.jpg')])+';library.forEach(p=>libraryByPath.set(p.path,p));selectedPaths=new Set(["/photos/a.jpg","/photos/c.jpg"]);');
    assert.equal(await p.run('applyBatchRating(2)'),2);assert.deepEqual(writes.map(item=>[item.path,item.rating]),[['/photos/a.jpg',2],['/photos/c.jpg',2]]);assert.equal(p.run('library[1].rating'),0);
  });
  await test('project removal persists an empty library without deleting originals',async()=>{
    const writes=[];const p=await page(async(path,body)=>path==='/api/project'&&body?(writes.push(body),{ok:true,saved_at:1}):{state:null});await tick();
    p.run('PROJECT.ready=true;PROJECT.blocked=false;PROJECT.folder="/photos";library=['+JSON.stringify(photo('/photos/a.jpg'))+'];libraryByPath.set(library[0].path,library[0]);selectedPaths=new Set([library[0].path]);');
    assert.equal(await p.run('removeSelectedFromProject()'),1);assert.equal(p.run('library.length'),0);assert.equal(writes.length,1);assert.equal(writes[0].photos.length,0);assert.match(p.confirmations[0],/本地原片.*都会保留/);
  });
  await test('trash action saves project removal before calling the local-trash endpoint',async()=>{
    const order=[];const p=await page(async(path,body)=>{if(body&&path==='/api/project'){order.push(['save',body.photos.length]);return {ok:true,saved_at:1};}if(path==='/api/trash'){order.push(['trash',body.paths]);return {removed:body.paths,failed:[],warnings:[]};}return {state:null};});await tick();
    p.run('PROJECT.ready=true;PROJECT.blocked=false;PROJECT.folder="/photos";library=['+JSON.stringify(photo('/photos/a.jpg'))+'];libraryByPath.set(library[0].path,library[0]);selectedPaths=new Set([library[0].path]);');
    assert.equal(await p.run('deleteSelectedToTrash()'),1);assert.equal(p.run('library.length'),0);assert.deepEqual(order,[['save',0],['trash',['/photos/a.jpg']]]);assert.match(p.confirmations[0],/移到 macOS 废纸篓/);
  });
  await test('local deletion never starts if project checkpointing fails',async()=>{
    const trashRequests=[];const p=await page(async(path,body)=>{if(path==='/api/project'&&body)return {ok:false};if(path==='/api/trash')trashRequests.push(body);return {state:null};});await tick();
    p.run('PROJECT.ready=true;PROJECT.blocked=false;PROJECT.folder="/photos";library=['+JSON.stringify(photo('/photos/a.jpg'))+'];libraryByPath.set(library[0].path,library[0]);selectedPaths=new Set([library[0].path]);');
    assert.equal(await p.run('deleteSelectedToTrash()'),0);assert.equal(p.run('library.length'),1);assert.equal(trashRequests.length,0);
  });
  await test('failed local-trash items are restored to the project and remain selected',async()=>{
    const writes=[];const a=photo('/photos/a.jpg'),b=photo('/photos/b.jpg');const p=await page(async(path,body)=>{if(path==='/api/project'&&body){writes.push(body);return {ok:true,saved_at:writes.length};}if(path==='/api/trash')return {removed:['/photos/a.jpg'],failed:[{path:'/photos/b.jpg',error:'拒绝访问'}],warnings:[]};return {state:null};});await tick();
    p.run('PROJECT.ready=true;PROJECT.blocked=false;PROJECT.folder="/photos";library='+JSON.stringify([a,b])+';library.forEach(p=>libraryByPath.set(p.path,p));selectedPaths=new Set(["/photos/a.jpg","/photos/b.jpg"]);');
    assert.equal(await p.run('deleteSelectedToTrash()'),1);assert.deepEqual(json(p,'library.map(p=>p.path)'),['/photos/b.jpg']);assert.equal(p.run('selectedPaths.has("/photos/b.jpg")'),true);assert.deepEqual(writes.map(state=>state.photos.length),[0,1]);
  });
  await test('canceling either destructive action makes no project or file request',async()=>{
    const requests=[];const p=await page(async(path,body)=>{requests.push({path,body});return path==='/api/project'?{state:null}:{ok:true};});await tick();p.setConfirm(false);
    p.run('PROJECT.ready=true;PROJECT.blocked=false;PROJECT.folder="/photos";library=['+JSON.stringify(photo('/photos/a.jpg'))+'];libraryByPath.set(library[0].path,library[0]);selectedPaths=new Set([library[0].path]);');
    assert.equal(await p.run('removeSelectedFromProject()'),0);assert.equal(await p.run('deleteSelectedToTrash()'),0);assert.deepEqual(requests.filter(request=>request.body).map(request=>request.path),[]);
  });
  await test('local deletion is blocked while a scan is reading selected originals',async()=>{
    const p=await page();await tick();p.run('PROJECT.ready=true;PROJECT.blocked=false;PROJECT.folder="/photos";library=['+JSON.stringify(photo('/photos/a.jpg'))+'];libraryByPath.set(library[0].path,library[0]);selectedPaths=new Set([library[0].path]);SCAN_ACTIVE=true;');
    assert.equal(await p.run('deleteSelectedToTrash()'),0);assert.equal(p.confirmations.length,0);assert.equal(p.run('library.length'),1);
  });
  await test('user intent during restore prevents control and library overwrites', async()=>{
    const saved=deferred();const p=await page(async path=>path==='/api/presets'?{presets:[{name:'natural'}]}:saved.promise);const state=await checkpoint(p);p.get('skin').value='.91';for(const fn of p.listeners.input||[])fn({isTrusted:true,target:p.get('skin')});saved.resolve({state});await tick();assert.equal(p.run('library.length'),0);assert.equal(+p.get('skin').value,.91);
  });
  for(const interaction of ['save','Tab','body click'])await test('benign '+interaction+' does not discard delayed startup restoration',async()=>{
    const saved=deferred();const p=await page(async path=>path==='/api/presets'?{presets:[{name:'natural'}]}:saved.promise);const state=await checkpoint(p);
    const event=interaction==='Tab'?{type:'keydown',key:'Tab',isTrusted:true}:{type:'pointerdown',target:p.get(interaction==='save'?'projectSave':'body'),isTrusted:true};
    for(const fn of p.listeners[event.type]||[])fn(event);
    if(interaction==='save')assert.equal(await p.run('saveProject(true)'),false);
    saved.resolve({state});await tick();
    assert.equal(p.run('library[0]?.path'),'/old.jpg');assert.equal(p.run('PROJECT.folder'),'/old');assert.equal(p.run('PROJECT.ready'),true);assert.equal(p.run('PROJECT.blocked'),false);
    assert.equal(p.get('projectRetry').hidden,true);assert.match(p.get('projectStatus').textContent,/已恢复 1 张/);
  });
  await test('density button mutation protects its value from delayed startup restoration',async()=>{
    const saved=deferred();const p=await page(async path=>path==='/api/presets'?{presets:[{name:'natural'}]}:saved.promise);const state=await checkpoint(p);
    p.get('viewbar').buttons.find(button=>button.dataset.density==='large').click();
    saved.resolve({state});await tick();assert.equal(p.get('grid').dataset.density,'large');assert.equal(p.run('library.length'),0);
  });
  for(const damaged of ['missing','corrupt'])await test('delayed '+damaged+' startup protects old checkpoint from a new partial scan', async()=>{
    const saved=deferred(),writes=[];
    const p=await page(async(path,body)=>path==='/api/presets'?{presets:[{name:'natural'}]}:path==='/api/project'?body?(writes.push(body),{ok:true}):saved.promise:path.startsWith('/api/scan_status')?{finished:false,total:2,done:1,photos:[photo('/new/a.jpg')]}:{job:'new',folder:'/new'});
    const state=await checkpoint(p);p.get('folder').value='/new';await p.run('scan()');await tick();
    if(damaged==='corrupt')saved.reject(Error('invalid project checkpoint'));else saved.resolve({state,missing:['/old.jpg']});
    await tick();assert.equal(p.run('library[0].path'),'/new/a.jpg');assert.equal(p.run('PROJECT.blocked'),true);assert.equal(await p.run('saveProject()'),false);assert.equal(writes.length,0);
    p.run('finishProjectScan(true)');assert.equal(await p.run('saveProject()'),true);assert.equal(writes.at(-1).folder,'/new');assert.equal(writes.at(-1).scan_incomplete,false);
  });
  for(const damaged of ['missing','corrupt'])await test('successful new terminal scan supersedes unresolved '+damaged+' startup protection', async()=>{
    const saved=deferred(),writes=[];
    const p=await page(async(path,body)=>path==='/api/presets'?{presets:[{name:'natural'}]}:path==='/api/project'?body?(writes.push(body),{ok:true}):saved.promise:path.startsWith('/api/scan_status')?{finished:true,total:1,done:1,refine_pending:false,final:[photo('/new/a.jpg')]}:{job:'new',folder:'/new'});
    const state=await checkpoint(p);p.get('folder').value='/new';await p.run('scan()');await tick();assert.equal(await p.run('saveProject()'),true);
    if(damaged==='corrupt')saved.reject(Error('invalid project checkpoint'));else saved.resolve({state,missing:['/old.jpg']});
    await tick();assert.equal(p.run('PROJECT.blocked'),false);assert.equal(p.run('PROJECT.ready'),true);assert.equal(p.run('library[0].path'),'/new/a.jpg');assert.equal(writes.at(-1).folder,'/new');
  });
  await test('healthy restore reports the number of restored photos',async()=>{
    const saved=deferred();const p=await page(async path=>path==='/api/presets'?{presets:[{name:'natural'}]}:saved.promise);const state=await checkpoint(p);saved.resolve({state});await tick();assert.match(p.get('projectStatus').textContent,/已恢复 1 张/);
  });
  await test('skipped terminal photos keep blocked recovery protected and incomplete',async()=>{
    const saved=deferred(),writes=[];
    const p=await page(async(path,body)=>path==='/api/presets'?{presets:[{name:'natural'}]}:path==='/api/project'?body?(writes.push(body),{ok:true}):saved.promise:path.startsWith('/api/scan_status')?{finished:true,total:2,done:2,skipped:1,refine_pending:false,final:[photo('/new/a.jpg')]}:{job:'new',folder:'/new'});
    const state=await checkpoint(p);saved.resolve({state,missing:['/old.jpg']});await tick();p.get('folder').value='/new';await p.run('scan()');await tick();assert.equal(p.run('PROJECT.blocked'),true);assert.equal(p.run('PROJECT.scanIncomplete'),true);assert.equal(await p.run('saveProject()'),false);assert.equal(writes.length,0);
  });
  await test('failed upload subset cannot unlock blocked checkpoint even with a complete subset scan',async()=>{
    const saved=deferred(),writes=[];let uploaded=0;
    const p=await page(async(path,body)=>path==='/api/presets'?{presets:[{name:'natural'}]}:path==='/api/project'?body?(writes.push(body),{ok:true}):saved.promise:path.startsWith('/api/scan_status')?{finished:true,total:1,done:1,skipped:0,refine_pending:false,final:[photo('/upload/a.jpg')]}:{job:'new',folder:'/upload'});
    const state=await checkpoint(p);saved.resolve({state,missing:['/old.jpg']});await tick();
    p.context.fetch=async(path,opts)=>path==='/api/upload'?{ok:true,json:async()=>++uploaded===1?{folder:'/upload'}:{error:'上传失败'}}:{ok:true,json:async()=>path==='/api/project'?(writes.push(JSON.parse(opts.body)),{ok:true}):path.startsWith('/api/scan_status')?{finished:true,total:1,done:1,skipped:0,refine_pending:false,final:[photo('/upload/a.jpg')]}:{job:'new',folder:'/upload'}};
    p.context.files=[{name:'a.jpg'},{name:'b.jpg'}];await p.run('uploadFiles(files)');await tick();await p.run('poll(POLL_GEN)');assert.equal(p.run('PROJECT.blocked'),true,'subset upload must stay blocked');assert.equal(p.run('PROJECT.scanIncomplete'),true,'subset upload must be incomplete');assert.equal(await p.run('saveProject()'),false);assert.equal(writes.length,0);
  });
  await test('large missing-file warnings show counts and a bounded safe path sample',async()=>{
    const saved=deferred();const p=await page(async path=>path==='/api/presets'?{presets:[{name:'natural'}]}:saved.promise);const state=await checkpoint(p);
    const missing=Array.from({length:2000},(_,i)=>'/<missing>/'+i+'x'.repeat(300));saved.resolve({state,missing,changed:[]});await tick();const status=p.get('projectStatus').textContent;assert.match(status,/缺失 2000 张/);assert.match(status,/<missing>/);assert.match(status,/另 1995/);assert.ok(status.length<1200);assert.equal(p.get('projectRetry').hidden,false);
  });
  for(const damaged of ['missing','corrupt'])await test('explicit startup folder protects '+damaged+' old checkpoint until a complete new scan',async()=>{
    const saved=deferred(),writes=[];let complete=false;
    const p=await page(async(path,body)=>path==='/api/presets'?{presets:[{name:'natural'}]}:path==='/api/project'?body?(writes.push(body),{ok:true}):saved.promise:path.startsWith('/api/scan_status')?complete?{finished:true,total:1,done:1,refine_pending:false,final:[photo('/explicit/a.jpg')]}:{finished:false,total:2,done:1,photos:[photo('/explicit/a.jpg')]}:{job:'explicit',folder:'/explicit'},'/explicit');
    const state=await checkpoint(p);state.settings.recipe.sliders.skin=.79;await tick();await p.run('poll(POLL_GEN)');
    if(damaged==='corrupt')saved.reject(Error('invalid project checkpoint'));else saved.resolve({state,missing:['/old.jpg']});
    await tick();assert.equal(p.run('PROJECT.blocked'),true);assert.equal(p.run('PROJECT.folder'),'/explicit');assert.notEqual(+p.get('skin').value,.79);assert.equal(p.run('library[0].path'),'/explicit/a.jpg');assert.equal(await p.run('saveProject()'),false);assert.equal(writes.length,0);
    complete=true;await p.run('poll(POLL_GEN)');assert.equal(p.run('PROJECT.blocked'),false);assert.equal(await p.run('saveProject()'),true);assert.equal(writes.at(-1).folder,'/explicit');assert.equal(writes.at(-1).scan_incomplete,false);
  });
  await test('an older healthy restore cannot unlock a newer missing-file recovery',async()=>{
    const initial=deferred(),older=deferred(),latest=deferred(),requests=[initial,older,latest],writes=[];let next=0;
    const p=await page(async(path,body)=>path==='/api/presets'?{presets:[{name:'natural'}]}:body?(writes.push(body),{ok:true}):requests[next++].promise);
    const state=await checkpoint(p,[photo('/good.jpg'),photo('/missing.jpg')]);initial.resolve({state,missing:['/missing.jpg']});await tick();
    const a=p.run('restoreProject()'),b=p.run('restoreProject()');latest.resolve({state,missing:['/missing.jpg']});await b;assert.equal(p.run('PROJECT.blocked'),true);
    older.resolve({state,missing:[]});await a;assert.equal(p.run('PROJECT.blocked'),true);assert.equal(p.run('library.length'),1);assert.equal(await p.run('saveProject()'),false);assert.equal(writes.length,0);
  });
  await test('an older failed restore cannot reblock the latest healthy recovery',async()=>{
    const initial=deferred(),older=deferred(),latest=deferred(),requests=[initial,older,latest];let next=0;
    const p=await page(async path=>path==='/api/presets'?{presets:[{name:'natural'}]}:requests[next++].promise);const state=await checkpoint(p);initial.resolve({state,missing:['/old.jpg']});await tick();
    const a=p.run('restoreProject()'),b=p.run('restoreProject()');latest.resolve({state,missing:[]});await b;const status=p.get('projectStatus').textContent;
    older.reject(Error('older request failed'));await a;assert.equal(p.run('PROJECT.blocked'),false);assert.equal(p.run('PROJECT.ready'),true);assert.equal(p.run('library.length'),1);assert.equal(p.get('projectStatus').textContent,status);
  });
  await test('an older restore finalizer cannot mark a newer pending restore ready',async()=>{
    const initial=deferred(),older=deferred(),latest=deferred(),requests=[initial,older,latest];let next=0;
    const p=await page(async path=>path==='/api/presets'?{presets:[{name:'natural'}]}:requests[next++].promise);const state=await checkpoint(p);initial.resolve({state});await tick();
    const a=p.run('restoreProject()'),b=p.run('restoreProject()');older.resolve({state});await a;assert.equal(p.run('PROJECT.ready'),false);assert.equal(p.run('PROJECT.blocked'),true);latest.resolve({state});await b;
  });
  await test('a slider drag is one undoable step and duplicate terminal events are noops',async()=>{
    const p=await editorPage();await p.open();const n=p.get('edit-exposure');n.onpointerdown?.({pointerId:1});
    for(let i=1;i<=40;i++){n.value=String(i/20);n.oninput();}n.onpointerup?.({pointerId:1});n.onchange?.();n.onblur?.();
    assert.equal(p.run('CURRENT_EDITOR.history.length'),2);assert.equal(p.get('editUndo').disabled,false);p.run('undoEditorTone()');assert.equal(+n.value,0);assert.equal(+p.get('tone-exposure').value,0);assert.equal(p.get('editRedo').disabled,false);
    p.run('redoEditorTone()');assert.equal(+n.value,2);assert.equal(p.get('editRedo').disabled,true);
  });
  await test('local retouch warnings remain visible in initial preview refreshed preview and export',async()=>{
    const warning='未检测到人脸：人脸局部修复未应用';
    const p=await editorPage((path)=>path==='/api/process_one'?{before:'/original.jpg',after:'/preview.jpg',out:'/out/final.jpg',width:100,height:80,warnings:[warning]}:undefined);
    await p.open();assert.match(p.get('editWarnings').textContent,/未检测到人脸/);assert.equal(p.get('editWarnings').hidden,false);
    const html=p.get('resInner').innerHTML;assert.ok(html.indexOf('id="editWarnings"')<html.indexOf('id="editToneControls"'),'diagnostics must precede controls so they are visible on short windows');
    p.drag(.5);await p.run('refreshEditorPreview()');assert.match(p.get('editWarnings').textContent,/未检测到人脸/);
    await p.run('exportEditor()');assert.match(p.get('editWarnings').textContent,/未检测到人脸/);assert.match(p.get('editStatus').textContent,/已导出/);
  });
  await test('a fresh preview clears old warnings and warning text never becomes HTML',async()=>{
    let warnings=['<img src=x onerror=alert(1)>'];
    const p=await editorPage(path=>path==='/api/process_one'?{before:'/original.jpg',after:'/preview.jpg',warnings}:undefined);
    await p.open();assert.equal(p.get('editWarnings').textContent,warnings[0]);assert.equal(p.get('editWarnings').innerHTML,'');
    warnings=[];p.drag(.5);await p.run('refreshEditorPreview()');assert.equal(p.get('editWarnings').hidden,true);assert.equal(p.get('editWarnings').textContent,'');
  });
  await test('an older preview cannot replace newer export diagnostics',async()=>{
    const preview=deferred();let hold=false;
    const p=await editorPage((path,body)=>path==='/api/process_one'?(body.preview_only&&hold?preview.promise:{before:'/original.jpg',after:'/preview.jpg',out:'/out/final.jpg',warnings:body.preview_only?['old']:['export warning']}):undefined);
    await p.open();p.drag(.5);hold=true;const work=p.run('refreshEditorPreview()');await tick();await p.run('exportEditor()');
    preview.resolve({after:'/late.jpg',warnings:['stale warning']});await work;assert.equal(p.get('editWarnings').textContent,'export warning');
  });
  await test('range keyboard autorepeat commits once at keyup and reset remains undoable',async()=>{
    const p=await editorPage();await p.open();const n=p.get('edit-exposure');n.onkeydown?.({key:'ArrowRight'});for(let i=1;i<=20;i++){n.value=String(i/20);n.oninput();n.onchange?.();}n.onkeyup?.({key:'ArrowRight'});
    assert.equal(p.run('CURRENT_EDITOR.history.length'),2);p.run('resetEditorTone()');assert.equal(+n.value,0);p.run('undoEditorTone()');assert.equal(+n.value,1);
  });
  await test('tone history caps at fifty states and a new edit truncates the redo branch',async()=>{
    const p=await editorPage();await p.open();for(let i=1;i<=60;i++)p.drag(i/20);
    assert.equal(p.run('CURRENT_EDITOR.history.length'),50);assert.equal(p.run('CURRENT_EDITOR.index'),49);p.run('undoEditorTone();undoEditorTone();');p.drag(.25);assert.equal(p.get('editRedo').disabled,true);assert.equal(p.run('CURRENT_EDITOR.history.length'),49);
  });
  await test('pending gesture flushes before undo and project snapshot including autosave',async()=>{
    const p=await editorPage();await p.open();p.drag(.7,false);const snapshot=json(p,'projectSnapshot()');assert.equal(snapshot.drafts['/photos/a.jpg'].tone.exposure,.7);assert.equal(snapshot.drafts['/photos/a.jpg'].history.length,2);
    p.drag(1.2,false);p.run('undoEditorTone()');assert.equal(+p.get('edit-exposure').value,.7);assert.equal(+p.get('tone-exposure').value,.7);
    p.drag(1.4,false);await p.run('saveProject()');const saved=p.requests.filter(r=>r.path==='/api/project'&&r.body).at(-1).body;assert.equal(saved.drafts['/photos/a.jpg'].tone.exposure,1.4);assert.equal(saved.settings.recipe.tone.exposure,1.4);
  });
  await test('closing persists photo history and reopening uses a fresh session with restored tone in its first preview',async()=>{
    const p=await editorPage();await p.open();const old=p.run('CURRENT_EDITOR.sid');p.drag(.9,false);p.run('closeResults()');const draft=json(p,'EDIT_DRAFTS.get("/photos/a.jpg")');assert.equal(draft.history.length,2);assert.equal('sid' in draft,false);
    await p.open('/photos/b.jpg');p.drag(-.5);p.run('closeResults()');await p.open();assert.notEqual(p.run('CURRENT_EDITOR.sid'),old);assert.equal(+p.get('edit-exposure').value,.9);assert.equal(p.run('CURRENT_EDITOR.history.length'),2);
    const first=p.requests.filter(r=>r.path==='/api/process_one'&&r.body.path==='/photos/a.jpg').at(-1);assert.equal(first.body.adjustments.exposure,.9);p.run('undoEditorTone()');assert.equal(+p.get('edit-exposure').value,0);assert.equal(json(p,'EDIT_DRAFTS.get("/photos/b.jpg")').tone.exposure,-.5);
  });
  await test('draft restore does not count programmatic initialization as new project intent',async()=>{
    const p=await editorPage();await p.open();p.drag(.5);p.run('closeResults()');const intent=p.run('PROJECT.intent');await p.open();assert.equal(p.run('PROJECT.intent'),intent);
  });
  await test('different operation or non-tone base does not reuse incompatible photo history',async()=>{
    const p=await editorPage();await p.open();p.drag(.8);p.run('closeResults()');p.get('skin').value='.9';await p.open();assert.equal(p.run('CURRENT_EDITOR.history.length'),1);p.drag(.4);p.run('closeResults()');await p.open('/photos/a.jpg','preset');assert.equal(p.run('CURRENT_EDITOR.history.length'),1);
  });
  await test('color-only editor disables tone history and preserves an existing polish draft',async()=>{
    const p=await editorPage();await p.open();p.drag(.6);p.run('closeResults()');const draft=json(p,'EDIT_DRAFTS.get("/photos/a.jpg")');p.run('targetPath="/photos/ref.jpg"');await p.open('/photos/a.jpg','color');assert.equal(p.get('editUndo').disabled,true);assert.equal(p.get('editRedo').disabled,true);assert.equal(p.get('editReset').disabled,true);p.run('undoEditorTone();resetEditorTone();closeResults()');assert.deepEqual(json(p,'EDIT_DRAFTS.get("/photos/a.jpg")'),draft);
  });
  await test('invalid draft bodies are ignored without rendering invalid values',async()=>{
    const p=await editorPage();await p.open();p.drag(.5);p.run('closeResults()');const good=json(p,'EDIT_DRAFTS.get("/photos/a.jpg")');
    for(const mutate of [d=>d.tone.exposure=9,d=>d.history=[],d=>d.index=50,d=>d.history=Array(51).fill(d.tone),d=>d.history[0].contrast='bad']){const d=structuredClone(good);mutate(d);p.context.invalid=d;p.run('EDIT_DRAFTS.set("/photos/a.jpg",invalid)');await p.open();assert.equal(p.run('CURRENT_EDITOR.history.length'),1);assert.ok(+p.get('edit-exposure').value<=3);p.run('closeResults()');}
  });
  await test('editor shortcuts honor native text undo and operate only while the editor is open',async()=>{
    const p=await editorPage();await p.open();p.drag(.8);let prevented=0;const key=(key,target={},extra={})=>{for(const fn of p.listeners.keydown||[])fn({key,ctrlKey:true,target,preventDefault(){prevented++;},...extra});};
    key('z',{tagName:'INPUT',type:'text'});key('z',{tagName:'TEXTAREA'});key('z',{isContentEditable:true});assert.equal(+p.get('edit-exposure').value,.8);assert.equal(prevented,0);
    key('z',{tagName:'INPUT',type:'range'});assert.equal(+p.get('edit-exposure').value,0);key('Z',{}, {shiftKey:true});assert.equal(+p.get('edit-exposure').value,.8);key('z');key('y');assert.equal(+p.get('edit-exposure').value,.8);assert.equal(prevented,4);p.run('closeResults()');key('z');assert.equal(prevented,4);
  });
  await test('stale preview failures cannot overwrite the newest preview status or closed editor',async()=>{
    const pending=deferred();let hold=false;const p=await editorPage((path,body)=>path==='/api/process_one'&&hold?pending.promise:undefined);await p.open();const statuses=[];let status='';Object.defineProperty(p.get('editStatus'),'textContent',{get:()=>status,set:value=>{status=value;statuses.push(value);}});hold=true;p.drag(.5);const work=p.run('refreshEditorPreview()');await tick();assert.equal(p.run('CURRENT_EDITOR.busy'),true);p.drag(.8);p.get('editStatus').textContent='newest';hold=false;pending.reject(Error('stale'));await work;await tick();assert.equal(statuses.some(value=>value.includes('stale')),false);assert.match(p.get('editStatus').textContent,/已更新/);
  });
  await test('export flushes the gesture freezes controls and renders current output settings in a fresh session',async()=>{
    const pending=deferred();let hold=false;const p=await editorPage((path,body)=>path==='/api/prepare'&&hold?pending.promise:undefined);await p.open();p.drag(.75,false);p.get('exportSize').value='original';p.get('exportQuality').value='100';hold=true;const exporting=p.run('exportEditor()');await tick();
    assert.equal(p.get('edit-exposure').disabled,true);assert.equal(p.get('tone-exposure').disabled,true);assert.equal(p.get('editReset').disabled,true);assert.equal(p.get('editUndo').disabled,true);p.run('resetEditorTone();undoEditorTone()');assert.equal(+p.get('edit-exposure').value,.75);
    const prep=p.requests.filter(r=>r.path==='/api/prepare').at(-1).body;assert.equal(prep.export_size,'original');assert.equal(prep.export_quality,100);assert.equal(json(p,'EDIT_DRAFTS.get("/photos/a.jpg")').history.length,2);
    pending.resolve({sid:'export-new',count:1,ref_desc:'测试'});await exporting;const rendered=p.requests.filter(r=>r.path==='/api/process_one').at(-1).body;assert.equal(rendered.sid,'export-new');assert.equal(rendered.adjustments.exposure,.75);assert.equal(p.get('edit-exposure').disabled,false);assert.equal(p.get('editUndo').disabled,false);
  });
  await test('closing during initial preparation or preview never reopens stale results',async()=>{
    const prep=deferred();let hold=true;const p=await editorPage((path)=>path==='/api/prepare'&&hold?prep.promise:undefined);const opening=p.open();await tick();p.run('closeResults()');prep.resolve({sid:'stale',count:1,ref_desc:'测试'});await opening;assert.equal(p.run('CURRENT_EDITOR'),null);assert.equal(p.get('results').classList.contains('show'),false);assert.equal(p.requests.filter(r=>r.path==='/api/process_one').length,0);
    hold=false;await p.open();const result=deferred();p.context.fetch=async()=>({ok:true,json:()=>result.promise});p.drag(.4);const work=p.run('refreshEditorPreview()');p.run('closeResults()');p.get('editStatus').textContent='closed';result.resolve({after:'/stale.jpg'});await work;assert.equal(p.get('editStatus').textContent,'closed');
  });
  await test('an older preview finishing after export preserves the download and exported status',async()=>{
    const preview=deferred();let hold=false;const p=await editorPage((path,body)=>path==='/api/process_one'&&body.preview_only&&hold?preview.promise:undefined);await p.open();p.drag(.4);hold=true;const work=p.run('refreshEditorPreview()');await tick();assert.equal(p.run('CURRENT_EDITOR.busy'),true);await p.run('exportEditor()');const status=p.get('editStatus').textContent,count=p.requests.length;hold=false;preview.resolve({after:'/late.jpg'});await work;await tick();assert.equal(p.get('editStatus').textContent,status);assert.match(status,/已导出/);assert.equal(p.requests.length,count);
  });
  await test('an adjustment after export refreshes when an older preview releases the busy editor',async()=>{
    const preview=deferred();let hold=false;const p=await editorPage((path,body)=>path==='/api/process_one'&&body.preview_only&&hold?preview.promise:undefined);await p.open();
    const timers=new Map();let nextTimer=0;p.context.setTimeout=(fn,delay)=>{const id=++nextTimer;if(delay===350)timers.set(id,fn);return id;};p.context.clearTimeout=id=>timers.delete(id);
    p.drag(.4);hold=true;const work=p.run('refreshEditorPreview()');await tick();await p.run('exportEditor()');const count=p.requests.length;
    p.drag(.8);assert.equal(timers.size,1);const [id,scheduled]=timers.entries().next().value;timers.delete(id);scheduled();await tick();assert.equal(p.requests.length,count,'busy editor must defer the new adjustment');
    hold=false;preview.resolve({after:'/late.jpg'});await work;await tick();assert.equal(p.requests.length,count+1,'latest adjustment must render after the old preview finishes');
    const rendered=p.requests.at(-1).body;assert.equal(rendered.preview_only,true);assert.equal(rendered.adjustments.exposure,.8);assert.equal(p.run('CURRENT_EDITOR.busy'),false);assert.equal(p.get('editStage').querySelector('img.b').src,'/preview.jpg');assert.match(p.get('editStatus').textContent,/已更新/);
  });
  await test('autosave snapshots during a continuous gesture keep exactly one final history step',async()=>{
    const p=await editorPage();await p.open();const input=p.get('edit-exposure');input.onpointerdown({pointerId:1});
    for(const value of [.2,.4,.6,.8]){input.value=String(value);input.oninput();const draft=json(p,'projectSnapshot().drafts["/photos/a.jpg"]');assert.equal(draft.history.length,2);assert.equal(draft.tone.exposure,value);}
    input.onpointerup({pointerId:1});assert.equal(p.run('CURRENT_EDITOR.history.length'),2);p.run('undoEditorTone()');assert.equal(+input.value,0);p.run('redoEditorTone()');assert.equal(+input.value,.8);
  });
  await test('a gesture returning to its start is a noop and preserves an existing redo branch',async()=>{
    const p=await editorPage();await p.open();p.drag(.8);p.run('undoEditorTone()');const input=p.get('edit-exposure');input.onpointerdown({pointerId:1});input.value='.4';input.oninput();p.run('projectSnapshot()');input.value='0';input.oninput();input.onpointerup({pointerId:1});assert.equal(p.run('CURRENT_EDITOR.history.length'),2);assert.equal(p.run('CURRENT_EDITOR.index'),0);assert.equal(p.get('editRedo').disabled,false);p.run('redoEditorTone()');assert.equal(+input.value,.8);
  });
  await test('saved draft restores its tone history and redo position across page reload',async()=>{
    const p=await editorPage();await p.open();p.drag(.4);p.drag(.9);p.run('undoEditorTone();closeResults()');const state=json(p,'projectSnapshot()');
    const restored=await editorPage((path,body)=>path==='/api/project'&&!body?{state}:undefined);await restored.open();assert.equal(+restored.get('edit-exposure').value,.4);assert.equal(restored.run('CURRENT_EDITOR.index'),1);assert.equal(restored.get('editRedo').disabled,false);restored.run('redoEditorTone()');assert.equal(+restored.get('edit-exposure').value,.9);
  });
  await test('a late preview error after export cannot replace the download status',async()=>{
    const preview=deferred();let hold=false;const p=await editorPage((path,body)=>path==='/api/process_one'&&body.preview_only&&hold?preview.promise:undefined);await p.open();p.drag(.4);hold=true;const work=p.run('refreshEditorPreview()');await tick();await p.run('exportEditor()');const status=p.get('editStatus').textContent,count=p.requests.length;hold=false;preview.reject(Error('late failed preview'));await work;await tick();assert.equal(p.get('editStatus').textContent,status);assert.match(status,/已导出/);assert.equal(p.requests.length,count);
  });
  await test('closing while the initial preview is rendering prevents result resurrection',async()=>{
    const preview=deferred();const p=await editorPage((path,body)=>path==='/api/process_one'&&body.preview_only?preview.promise:undefined);const work=p.open();await tick();p.run('closeResults()');preview.resolve({before:'/before',after:'/late'});await work;assert.equal(p.run('CURRENT_EDITOR'),null);assert.equal(p.get('results').classList.contains('show'),false);
  });
  await test('a current preview error remains visible and the next adjustment retries',async()=>{
    let fail=false;const p=await editorPage((path,body)=>path==='/api/process_one'&&body.preview_only&&fail?Promise.reject(Error('preview unavailable')):undefined);await p.open();fail=true;p.drag(.3);await p.run('refreshEditorPreview()');assert.match(p.get('editStatus').textContent,/preview unavailable/);fail=false;p.drag(.6);await p.run('refreshEditorPreview()');assert.match(p.get('editStatus').textContent,/已更新/);
  });
  await test('global tone reset cannot mutate controls or intent while export is rendering',async()=>{
    const pending=deferred();let hold=false;const p=await editorPage(path=>path==='/api/prepare'&&hold?pending.promise:undefined);await p.open();p.drag(.75);hold=true;const exporting=p.run('exportEditor()');await tick();const intent=p.run('PROJECT.intent');p.run('resetTone()');assert.equal(+p.get('tone-exposure').value,.75);assert.equal(+p.get('edit-exposure').value,.75);assert.equal(p.run('PROJECT.intent'),intent);pending.resolve({sid:'export-frozen',count:1});await exporting;assert.equal(p.requests.filter(r=>r.path==='/api/process_one').at(-1).body.adjustments.exposure,.75);
  });
  await test('a parameter file that finishes reading during export cannot replace the captured tone',async()=>{
    const reading=deferred(),rendering=deferred();let hold=false;const p=await editorPage(path=>path==='/api/prepare'&&hold?rendering.promise:undefined);await p.open();p.drag(.75);const recipe=json(p,'recipeValues()');recipe.tone.exposure=-.5;p.context.file={size:100,text:()=>reading.promise};const loading=p.run('loadRecipe(file)');hold=true;const exporting=p.run('exportEditor()');await tick();const intent=p.run('PROJECT.intent');reading.resolve(JSON.stringify(recipe));await loading;assert.equal(+p.get('tone-exposure').value,.75);assert.equal(+p.get('edit-exposure').value,.75);assert.equal(p.run('PROJECT.intent'),intent);rendering.resolve({sid:'export-frozen',count:1});await exporting;assert.equal(p.requests.filter(r=>r.path==='/api/process_one').at(-1).body.adjustments.exposure,.75);
  });
  await test('parameter loading cannot even start reading a file while export is active',async()=>{
    const rendering=deferred();let hold=false,reads=0;const p=await editorPage(path=>path==='/api/prepare'&&hold?rendering.promise:undefined);await p.open();p.drag(.75);const recipe=json(p,'recipeValues()');recipe.tone.exposure=-.5;p.context.file={size:100,text:async()=>{reads++;return JSON.stringify(recipe);}};hold=true;const exporting=p.run('exportEditor()');await tick();await p.run('loadRecipe(file)');assert.equal(reads,0);assert.equal(+p.get('tone-exposure').value,.75);rendering.resolve({sid:'export-frozen',count:1});await exporting;
  });
  for(const ending of ['complete','close'])await test('export locks global tone actions and restores their prior disabled states on '+ending,async()=>{
    const rendering=deferred();let hold=false;const p=await editorPage(path=>path==='/api/prepare'&&hold?rendering.promise:undefined);await p.open();const buttons=p.get('toneActions').buttons;buttons[1].disabled=true;p.get('recipeFile').disabled=ending==='close';hold=true;const exporting=p.run('exportEditor()');await tick();assert.ok(buttons.every(button=>button.disabled));assert.equal(p.get('recipeFile').disabled,true);if(ending==='close')p.run('closeResults()');rendering.resolve({sid:'export-frozen',count:1});await exporting;assert.deepEqual(buttons.map(button=>button.disabled),[false,true,false]);assert.equal(p.get('recipeFile').disabled,ending==='close');
  });
  await test('portrait controls send current photo retouch without changing opening settings or global controls',async()=>{
    const p=await editorPage();await p.open();const opening=json(p,'CURRENT_EDITOR.settings');const global=json(p,'recipeValues()');
    for(const [key,value] of Object.entries({skin:.72,retain:.64,clarity:.36,face_repair:.8,blemish:.6}))p.retouch(key,value);
    p.region('eyes');await p.run('refreshEditorPreview()');const body=p.requests.at(-1).body;
    assert.deepEqual(body.retouch_adjustments,{skin:.72,retain:.64,clarity:.36,face_repair:.8,blemish:.6,local_region:'eyes'});assert.equal('show_mask' in body,false);
    assert.deepEqual(json(p,'CURRENT_EDITOR.settings'),opening);assert.deepEqual(json(p,'recipeValues()'),global);
    assert.equal(p.get('editToneTab').getAttribute?.('aria-selected')||p.get('editToneTab').ariaSelected,'true');
    p.run('selectEditorPanel("portrait")');assert.equal(p.get('editPortraitControls').hidden,false);assert.equal(p.get('editTonePanel').hidden,true);
  });
  await test('portrait gestures autosave as one unified undo step and region reset are undoable',async()=>{
    const p=await editorPage();await p.open();const initial=json(p,'CURRENT_EDITOR.retouch'),n=p.get('edit-retouch-skin');n.onpointerdown();
    for(const value of [.2,.4,.8]){n.value=String(value);n.oninput();const d=json(p,'projectSnapshot().drafts["/photos/a.jpg"]');assert.equal(d.version,2);assert.equal(d.retouch.skin,value);assert.equal(d.history.length,2);assert.equal(d.retouchHistory.length,2);}
    n.onpointerup();n.onchange();n.onblur();assert.equal(p.run('CURRENT_EDITOR.history.length'),2);p.run('undoEditorTone()');assert.equal(+n.value,initial.skin);p.run('redoEditorTone()');assert.equal(+n.value,.8);
    p.drag(.5);p.region('face');p.run('undoEditorTone()');assert.equal(p.get('editRegion').value,initial.local_region);assert.equal(+p.get('edit-exposure').value,.5);p.run('redoEditorTone();resetEditorRetouch()');assert.deepEqual(json(p,'CURRENT_EDITOR.retouch'),initial);assert.equal(+p.get('edit-exposure').value,.5);
    p.run('undoEditorTone();resetEditorTone()');assert.equal(+n.value,.8);assert.equal(p.get('editRegion').value,'face');assert.equal(+p.get('edit-exposure').value,0);
  });
  await test('parallel retouch history caps truncates and restores redo after a no-op gesture',async()=>{
    const p=await editorPage();await p.open();for(let i=1;i<=60;i++)p.retouch('skin',(i%10)/10);
    assert.equal(p.run('CURRENT_EDITOR.history.length'),50);assert.equal(p.run('CURRENT_EDITOR.retouchHistory.length'),50);p.run('undoEditorTone();undoEditorTone()');const before=json(p,'CURRENT_EDITOR.retouch');const n=p.get('edit-retouch-skin');n.onpointerdown();n.value='.35';n.oninput();p.run('projectSnapshot()');n.value=String(before.skin);n.oninput();n.onpointerup();assert.equal(p.run('CURRENT_EDITOR.index'),47);assert.equal(p.get('editRedo').disabled,false);
    p.retouch('skin',.37);assert.equal(p.run('CURRENT_EDITOR.history.length'),49);assert.equal(p.run('CURRENT_EDITOR.retouchHistory.length'),49);assert.equal(p.get('editRedo').disabled,true);
  });
  await test('v2 draft restores retouch redo across close reopen and page restore',async()=>{
    const p=await editorPage();await p.open();p.retouch('skin',.6);p.region('face');p.run('undoEditorTone();closeResults()');const state=json(p,'projectSnapshot()');await p.open();assert.equal(+p.get('edit-retouch-skin').value,.6);assert.equal(p.requests.at(-1).body.retouch_adjustments.skin,.6);assert.equal(p.get('editMask').checked,false);p.run('closeResults()');
    const restored=await editorPage((path,body)=>path==='/api/project'&&!body?{state}:undefined);await restored.open();assert.equal(+restored.get('edit-retouch-skin').value,.6);assert.equal(restored.get('editRedo').disabled,false);restored.run('redoEditorTone()');assert.equal(restored.get('editRegion').value,'face');
  });
  await test('v1 draft migration fills frozen initial retouch throughout existing tone history',async()=>{
    const p=await editorPage();await p.open();const initial=json(p,'CURRENT_EDITOR.retouch');p.drag(.4);p.drag(.8);p.run('undoEditorTone();closeResults()');const d=json(p,'EDIT_DRAFTS.get("/photos/a.jpg")');d.version=1;delete d.retouch;delete d.retouchHistory;p.context.legacy=d;p.run('EDIT_DRAFTS.set("/photos/a.jpg",legacy)');await p.open();assert.equal(+p.get('edit-exposure').value,.4);assert.deepEqual(json(p,'CURRENT_EDITOR.retouchHistory'),[initial,initial,initial]);p.run('redoEditorTone()');assert.equal(+p.get('edit-exposure').value,.8);assert.deepEqual(json(p,'CURRENT_EDITOR.retouch'),initial);
  });
  await test('invalid v2 retouch data is rejected atomically with its tone history',async()=>{
    const p=await editorPage();await p.open();p.drag(.7);p.retouch('skin',.8);p.run('closeResults()');const good=json(p,'EDIT_DRAFTS.get("/photos/a.jpg")');
    for(const mutate of [d=>d.retouch.skin=2,d=>d.retouch.extra=1,d=>d.retouch.local_region='nose',d=>d.retouchHistory.pop(),d=>d.retouchHistory[0].blemish='bad',d=>d.retouchHistory[d.index].skin=.4,d=>d.version=3]){const d=structuredClone(good);mutate(d);p.context.invalid=d;p.run('EDIT_DRAFTS.set("/photos/a.jpg",invalid)');const openingTone=+p.get('tone-exposure').value;await p.open();assert.equal(p.run('CURRENT_EDITOR.history.length'),1);assert.equal(+p.get('edit-exposure').value,openingTone);assert.equal(p.run('CURRENT_EDITOR.retouchHistory.length'),1);assert.equal(+p.get('edit-retouch-skin').value,+p.get('skin').value);p.run('closeResults()');}
  });
  for(const ending of ['complete','close'])await test('export freezes latest retouch locks every portrait control and restores prior states on '+ending,async()=>{
    const pending=deferred();let hold=false;const p=await editorPage(path=>path==='/api/prepare'&&hold?pending.promise:undefined);await p.open();const base=json(p,'CURRENT_EDITOR.settings');p.retouch('skin',.83,false);p.region('background');p.get('editMask').disabled=true;hold=true;const work=p.run('exportEditor()');await tick();
    for(const id of ['edit-retouch-skin','edit-retouch-retain','edit-retouch-clarity','edit-retouch-face_repair','edit-retouch-blemish','editRegion','editMask','editRetouchReset'])assert.equal(p.get(id).disabled,true,id);
    const prep=p.requests.filter(r=>r.path==='/api/prepare').at(-1).body;assert.equal(prep.polish.skin,.83);assert.equal(prep.polish.local_region,'background');assert.equal('show_mask' in prep,false);assert.deepEqual(json(p,'CURRENT_EDITOR.settings'),base);
    p.retouch('skin',.2);p.run('resetEditorRetouch()');assert.equal(p.run('CURRENT_EDITOR.retouch.skin'),.83);if(ending==='close')p.run('closeResults()');pending.resolve({sid:'frozen',count:1});await work;assert.equal(p.get('edit-retouch-skin').disabled,false);assert.equal(p.get('editMask').disabled,true);
    if(ending==='complete'){const out=p.requests.at(-1).body;assert.equal(out.sid,'frozen');assert.equal('show_mask' in out,false);assert.equal('retouch_adjustments' in out,false);}
  });
  await test('mask waits for corresponding frame and mask decode and rejects late decode callbacks',async()=>{
    let sequence=0;const p=await editorPage(path=>path==='/api/process_one'?{before:'/before',after:'/frame'+ ++sequence,region_mask:'/mask'+sequence}:undefined);await p.open();p.mask(true);await p.run('refreshEditorPreview()');assert.equal(p.requests.at(-1).body.show_mask,true);const mask=p.get('editRegionMask'),image=p.get('editStage').querySelector('img.b');assert.equal(mask.hidden,true);
    const oldAfter=p.images.find(i=>i.src==='/frame2'),oldMask=p.images.find(i=>i.src==='/mask2');assert.ok(oldAfter&&oldMask);const oldDisplayed=image.onload,oldDisplayedMask=mask.onload;oldMask.onload();assert.equal(mask.hidden,true);oldAfter.onload();assert.equal(mask.hidden,true);oldDisplayed();assert.equal(mask.hidden,true);oldDisplayedMask();assert.equal(mask.hidden,false);assert.equal(mask.src,'/mask2');
    p.region('eyes');assert.equal(mask.hidden,true);await p.run('refreshEditorPreview()');oldAfter.onload();oldMask.onload();oldDisplayed();oldDisplayedMask();assert.equal(mask.hidden,true);const nextAfter=p.images.find(i=>i.src==='/frame3'),nextMask=p.images.find(i=>i.src==='/mask3');nextAfter.onload();image.onload();assert.equal(mask.hidden,true);nextMask.onload();assert.equal(mask.hidden,true);mask.onload();assert.equal(mask.hidden,false);assert.equal(mask.src,'/mask3');
    p.mask(false);assert.equal(mask.hidden,true);image.onload();nextMask.onload();assert.equal(mask.hidden,true);assert.equal(json(p,'projectSnapshot().drafts["/photos/a.jpg"]').showMask,undefined);
  });
  await test('mask disappears on failure close new editor export and stale response',async()=>{
    const pending=deferred();let hold=false,fail=false;const p=await editorPage((path,body)=>path==='/api/process_one'&&body.preview_only?(hold?pending.promise:fail?Promise.reject(Error('bad mask')):{before:'/before',after:'/frame',region_mask:'/mask'}):undefined);await p.open();p.mask(true);await p.run('refreshEditorPreview()');const image=p.get('editStage').querySelector('img.b'),mask=p.get('editRegionMask');p.images.at(-2).onload();image.onload();p.images.at(-1).onload();mask.onload();assert.equal(mask.hidden,false);
    fail=true;p.drag(.4);assert.equal(mask.hidden,true);await p.run('refreshEditorPreview()');assert.equal(mask.hidden,true);fail=false;hold=true;p.region('skin');const work=p.run('refreshEditorPreview()');p.mask(false);hold=false;pending.resolve({after:'/late',region_mask:'/late-mask'});await work;assert.equal(mask.hidden,true);
    const decode=p.images.at(-1)?.onload;p.run('closeResults()');decode?.();assert.equal(mask.hidden,true);await p.open('/photos/b.jpg');decode?.();assert.equal(p.get('editMask').checked,false);assert.equal(mask.hidden,true);
  });
  await test('mask loads accept browser absolute image URLs and refuse a load belonging to an older currentSrc',async()=>{
    const p=await editorPage(path=>path==='/api/process_one'?{before:'/before',after:'/current.jpg',region_mask:'/region.png'}:undefined);await p.open();p.mask(true);await p.run('refreshEditorPreview()');
    const image=p.get('editStage').querySelector('img.b'),mask=p.get('editRegionMask');image.setAttribute('src','/current.jpg');image.src='http://localhost/current.jpg';image.currentSrc='http://localhost/older.jpg';
    p.images.at(-2).onload();p.images.at(-1).onload();image.onload();assert.equal(mask.hidden,true);
    image.currentSrc=image.src;image.onload();assert.equal(mask.hidden,true);mask.setAttribute('src','/region.png');mask.src='http://localhost/region.png';mask.currentSrc='http://localhost/previous.png';mask.onload();assert.equal(mask.hidden,true);mask.currentSrc=mask.src;mask.onload();assert.equal(mask.hidden,false,'matching browser loads must reveal the decoded mask');
  });
  await test('displayed mask must load its new bitmap before reveal over the corresponding frame',async()=>{
    const p=await editorPage(path=>path==='/api/process_one'?{before:'/before',after:'/new.jpg',region_mask:'/new.png'}:undefined);await p.open();const mask=p.get('editRegionMask');mask.src='/previous.png';p.mask(true);await p.run('refreshEditorPreview()');
    const image=p.get('editStage').querySelector('img.b');p.images.at(-2).onload();image.onload();p.images.at(-1).onload();assert.equal(mask.hidden,true,'a previous displayed mask bitmap must stay hidden while the new bitmap loads');
    mask.onload();assert.equal(mask.hidden,false);assert.equal(mask.src,'/new.png');
  });
  await test('portrait reset preserves prior disabled state after export completion',async()=>{
    const p=await editorPage();await p.open();p.get('editRetouchReset').disabled=true;p.get('editRegion').disabled=true;await p.run('exportEditor()');assert.equal(p.get('editRetouchReset').disabled,true);assert.equal(p.get('editRegion').disabled,true);
  });
  await test('region selection participates in the same keyboard undo and redo stack',async()=>{
    const p=await editorPage();await p.open();const initial=p.get('editRegion').value;p.retouch('skin',.71);p.region('eyes');let prevented=0;
    for(const fn of p.listeners.keydown||[])fn({key:'z',ctrlKey:true,target:{tagName:'SELECT',id:'editRegion'},preventDefault(){prevented++;}});
    assert.equal(p.get('editRegion').value,initial);assert.equal(+p.get('edit-retouch-skin').value,.71);assert.equal(prevented,1);
  });
  await test('color-only editor has no portrait or mask controls and no retouch request',async()=>{
    const p=await editorPage();p.run('targetPath="/photos/ref.jpg"');await p.open('/photos/a.jpg','color');const html=p.get('resInner').innerHTML;assert.equal(html.includes('id="editRegion"'),false);assert.equal(html.includes('id="editMask"'),false);assert.equal(html.includes('id="editPortraitTab"'),false);assert.equal('retouch_adjustments' in p.requests.at(-1).body,false);
  });
  const exportJob=(id='export-one',state='queued',extra={})=>({id,state,created_at:1,updated_at:2,counts:{total:2,pending:2,running:0,succeeded:0,failed:0},...extra});
  async function exportPage(handler=null){
    const requests=[];const p=await page(async(path,body)=>{requests.push({path,body});const answer=handler?.(path,body);if(answer!==undefined)return answer;return path==='/api/presets'?{presets:[{name:'natural'}]}:path==='/api/project'?{state:null}:path==='/api/exports'?body?{job:exportJob()}:{jobs:[],queue_error:null}:path==='/api/prepare'?{sid:'single',count:1,ref_desc:'单张'}:{before:'/before',after:'/after',out:'/out.png',width:100,height:100};},'',true);
    await tick();p.requests=requests;p.get('folder').value='/photos';p.run('PROJECT.folder="/photos";library='+JSON.stringify([photo('/photos/a.png'),photo('/photos/b.png')])+';ranked=library;selectedPaths=new Set(library.map(p=>p.path));bindCmp=()=>{};');return p;
  }
  await test('batch processing submits a durable queue instead of browser-driven renders',async()=>{
    const p=await exportPage();await p.run('process("polish")');const submit=p.requests.find(r=>r.path==='/api/exports'&&r.body);
    assert.ok(submit,'batch must submit a durable export job');assert.deepEqual(submit.body.settings.paths,['/photos/a.png','/photos/b.png']);assert.equal(submit.body.settings.operation,'polish');assert.ok(submit.body.request_id);assert.equal(p.requests.some(r=>r.path==='/api/process_one'),false);assert.equal(p.run('PROCESS_BUSY'),false);assert.equal(p.get('results').classList.contains('show'),false);assert.equal(p.get('prog').classList.contains('show'),false);
  });
  await test('single-photo editing still uses prepare and preview without creating an export queue',async()=>{
    const p=await exportPage();p.run('selectedPaths=new Set([library[0].path]);');await p.run('process("polish")');assert.equal(p.requests.filter(r=>r.path==='/api/exports'&&r.body).length,0);assert.equal(p.requests.filter(r=>r.path==='/api/process_one').at(-1).body.preview_only,true);assert.equal(p.run('CURRENT_EDITOR.path'),'/photos/a.png');
  });
  await test('first queue read blocks a new batch while single-photo editing remains available',async()=>{
    const first=deferred();const p=await exportPage((path,body)=>path==='/api/exports'&&!body?first.promise:undefined);
    await p.run('process("polish")');
    assert.equal(p.requests.filter(r=>r.path==='/api/exports'&&r.body).length,0);
    assert.match(p.get('exportQueueError').textContent,/读取/);
    assert.equal(p.run('EXPORT_QUEUE.pending'),null);
    p.run('selectedPaths=new Set([library[0].path]);');await p.run('process("polish")');
    assert.equal(p.run('CURRENT_EDITOR.path'),'/photos/a.png');
    assert.equal(p.requests.filter(r=>r.path==='/api/process_one').at(-1).body.preview_only,true);
    first.resolve({jobs:[exportJob('restored','paused')]});await tick();
    assert.equal(p.run('EXPORT_QUEUE.jobs.has("restored")'),true);
    p.run('selectedPaths=new Set(library.map(p=>p.path));');await p.run('process("polish")');
    assert.equal(p.requests.filter(r=>r.path==='/api/exports'&&r.body).length,1);
  });
  await test('failed first queue read blocks new batches until manual refresh succeeds',async()=>{
    let reads=0;const p=await exportPage((path,body)=>path==='/api/exports'&&!body?++reads===1?Promise.reject(Error('首次读取失败')):{jobs:[exportJob('existing','running')]}:undefined);
    await p.run('process("polish")');
    assert.equal(p.requests.filter(r=>r.path==='/api/exports'&&r.body).length,0);
    assert.equal(p.run('EXPORT_QUEUE.pending'),null);
    assert.match(p.get('exportQueueError').textContent,/刷新队列/);
    await p.run('refreshExportQueue(true)');assert.equal(reads,2);
    assert.equal(p.run('EXPORT_QUEUE.jobs.has("existing")'),true);
    await p.run('process("polish")');
    assert.equal(p.requests.filter(r=>r.path==='/api/exports'&&r.body).length,1);
  });
  await test('single-photo editing preserves an ambiguous batch for explicit confirmation',async()=>{
    let failed=true;const submitted=[];const p=await exportPage((path,body)=>path==='/api/exports'&&body?(submitted.push(body),failed?(failed=false,Promise.reject(Error('lost'))):{job:exportJob()}):undefined);
    await p.run('process("polish")');const pending=json(p,'EXPORT_QUEUE.pending');
    p.run('selectedPaths=new Set([library[0].path]);');await p.run('process("polish")');
    assert.equal(p.run('CURRENT_EDITOR?.path'),'/photos/a.png');
    assert.equal(p.requests.filter(r=>r.path==='/api/process_one').at(-1).body.preview_only,true);
    assert.deepEqual(json(p,'EXPORT_QUEUE.pending'),pending);assert.equal(submitted.length,1);
    assert.equal(p.get('exportPendingRetry').hidden,false);
    await p.run('confirmPendingExport()');assert.equal(submitted.length,2);
    assert.equal(submitted[1].request_id,pending.request_id);assert.deepEqual(submitted[1].settings,pending.settings);
  });
  await test('page startup restores export summaries without automatically opening results',async()=>{
    const p=await exportPage((path,body)=>path==='/api/exports'&&!body?{jobs:[exportJob('restored','paused')],queue_error:null}:undefined);assert.ok(p.requests.some(r=>r.path==='/api/exports'&&!r.body));assert.equal(p.run('EXPORT_QUEUE.jobs.get("restored").state'),'paused');assert.equal(p.get('results').classList.contains('show'),false);assert.match(source,/id="exportQueue"/);
  });
  for(const action of ['pause','resume','retry'])await test('export '+action+' acts on frozen backend job identity',async()=>{
    const p=await exportPage((path,body)=>path==='/api/exports'&&!body?{jobs:[exportJob()],queue_error:null}:path==='/api/export_action'?{job:exportJob(body.id,body.action==='pause'?'paused':'queued')}:undefined);p.get('skin').value='.99';await p.run('exportAction("export-one",'+JSON.stringify(action)+')');const request=p.requests.find(r=>r.path==='/api/export_action');assert.deepEqual(request.body,{id:'export-one',action});assert.equal(p.requests.some(r=>r.path==='/api/prepare'),false);
  });
  await test('stale queue GET cannot resurrect forgotten terminal records',async()=>{
    const stale=deferred();let reads=0;const old=exportJob('finished','completed',{counts:{total:2,pending:0,running:0,succeeded:2,failed:0}});
    const p=await exportPage((path,body)=>path==='/api/exports'&&!body?++reads===1?{jobs:[old]}:stale.promise:path==='/api/export_action'?{removed:true}:undefined);const loading=p.run('refreshExportQueue()');await tick();await p.run('exportAction("finished","forget")');stale.resolve({jobs:[old]});await loading;assert.equal(p.run('EXPORT_QUEUE.jobs.has("finished")'),false);
  });
  await test('stale queue GET cannot overwrite a newer pause acknowledgement',async()=>{
    const stale=deferred();let reads=0;const p=await exportPage((path,body)=>path==='/api/exports'&&!body?++reads===1?{jobs:[exportJob()]}:stale.promise:path==='/api/export_action'?{job:exportJob('export-one','paused',{updated_at:4})}:undefined);const loading=p.run('refreshExportQueue()');await tick();await p.run('exportAction("export-one","pause")');stale.resolve({jobs:[exportJob('export-one','running',{updated_at:3})]});await loading;assert.equal(p.run('EXPORT_QUEUE.jobs.get("export-one").state'),'paused');
  });
  await test('queue refresh coalesces concurrent polling and exposes recoverable errors',async()=>{
    const delayed=deferred();let reads=0;const p=await exportPage((path,body)=>path==='/api/exports'&&!body?++reads===1?{jobs:[]}:delayed.promise:undefined);const a=p.run('refreshExportQueue()'),b=p.run('refreshExportQueue()');await tick();assert.equal(reads,2);delayed.reject(Error('暂时连接失败'));await Promise.all([a,b]);assert.match(p.get('exportQueueError').textContent,/暂时连接失败/);
  });
  await test('ambiguous export submission retries the same request identity and original settings',async()=>{
    const submitted=[];let failed=true;const p=await exportPage((path,body)=>path==='/api/exports'&&body?(submitted.push(body),failed?(failed=false,Promise.reject(Error('response lost'))):{job:exportJob()}):undefined);await p.run('process("polish")');p.get('skin').value='.95';await p.run('process("polish")');assert.equal(submitted.length,2);assert.equal(submitted[0].request_id,submitted[1].request_id);assert.deepEqual(submitted[0].settings,submitted[1].settings);assert.equal(p.run('PROCESS_BUSY'),false);
  });
  for(const status of [400,403,413])await test('definite HTTP '+status+' rejection releases invalid pending export settings',async()=>{
    const p=await exportPage();const submitted=[];let rejected=true;p.context.fetch=async(path,opts)=>{if(path==='/api/exports'&&opts?.body){const body=JSON.parse(opts.body);submitted.push(body);return rejected?(rejected=false,{ok:false,status,json:async()=>({error:'参数无效'})}):{ok:true,status:200,json:async()=>({job:exportJob()})};}return {ok:true,json:async()=>path==='/api/exports'?{jobs:[]}:{state:null}};};
    await p.run('process("polish")');p.get('skin').value='.95';await p.run('process("polish")');assert.equal(submitted.length,2);assert.notEqual(submitted[0].request_id,submitted[1].request_id);assert.equal(submitted[1].settings.polish.skin,.95);
  });
  await test('accepted export with an unreadable response keeps the original id and settings for retry',async()=>{
    const p=await exportPage();const submitted=[];let unreadable=true;p.context.fetch=async(path,opts)=>{if(path==='/api/exports'&&opts?.body){submitted.push(JSON.parse(opts.body));return {ok:true,status:200,json:async()=>{if(unreadable){unreadable=false;throw Error('invalid JSON response');}return {job:exportJob()};}};}return {ok:true,json:async()=>path==='/api/exports'?{jobs:[]}:{state:null}};};
    await p.run('process("polish")');p.get('skin').value='.95';await p.run('process("polish")');assert.equal(submitted.length,2);assert.equal(submitted[0].request_id,submitted[1].request_id);assert.deepEqual(submitted[0].settings,submitted[1].settings);
  });
  await test('queue count refresh preserves an existing job row and focused action button',async()=>{
    let count=0;const p=await exportPage((path,body)=>path==='/api/exports'&&!body?{jobs:[exportJob('stable','running',{counts:{total:2,pending:1,running:1,succeeded:count++,failed:0}})]}:undefined);
    const row=p.run('EXPORT_QUEUE.nodes.get("stable")');assert.ok(row);const button=row.parts.buttons.get('pause');p.context.document.activeElement=button;await p.run('refreshExportQueue()');assert.equal(p.run('EXPORT_QUEUE.nodes.get("stable")'),row);assert.equal(row.parts.buttons.get('pause'),button);assert.equal(p.context.document.activeElement,button);
  });
  await test('export result review is explicit and requests a bounded detail slice',async()=>{
    const p=await exportPage((path,body)=>path.startsWith('/api/export?')?{job:{...exportJob('done','completed'),items:[{path:'/photos/<a>.png',state:'succeeded',result:{before:'/api/thumb?p=source&s=640',after:'/api/thumb?p=output&s=640',download:'/api/img?p=output',out:'/out.png'}}],items_total:20000,offset:0}}:undefined);assert.equal(p.requests.some(r=>r.path.startsWith('/api/export?')),false);await p.run('openExportResults("done")');const url=p.requests.find(r=>r.path.startsWith('/api/export?')).path;assert.match(url,/limit=50/);assert.match(url,/offset=0/);assert.equal(p.get('results').classList.contains('show'),true);assert.ok(p.get('resInner').children.length<=52);const cards=p.get('resInner').children.map(child=>child.innerHTML).join('');assert.match(cards,/src="\/api\/thumb\?p=source&amp;s=640"/);assert.match(cards,/src="\/api\/thumb\?p=output&amp;s=640"/);assert.match(cards,/href="\/api\/img\?p=output"/);assert.doesNotMatch(cards,/<a>\.png/);
  });
  await test('unreadable HTTP error response preserves unconfirmed export identity',async()=>{
    const p=await exportPage();const submitted=[];let unreadable=true;p.context.fetch=async(path,opts)=>{if(path==='/api/exports'&&opts?.body){submitted.push(JSON.parse(opts.body));return {ok:!unreadable,status:unreadable?400:200,json:async()=>{if(unreadable){unreadable=false;throw Error('truncated JSON');}return {job:exportJob()};}};}return {ok:true,json:async()=>({jobs:[]})};};await p.run('process("polish")');await p.run('process("polish")');assert.equal(submitted[0].request_id,submitted[1].request_id);
  });
  await test('explicit confirmation retries an ambiguous batch even after selecting a single photo',async()=>{
    let failed=true;const submitted=[];const p=await exportPage((path,body)=>path==='/api/exports'&&body?(submitted.push(body),failed?(failed=false,Promise.reject(Error('lost'))):{job:exportJob()}):undefined);await p.run('process("polish")');p.run('selectedPaths=new Set([library[0].path]);');await p.run('confirmPendingExport()');assert.equal(submitted.length,2);assert.equal(submitted[0].request_id,submitted[1].request_id);assert.deepEqual(submitted[1].settings.paths,['/photos/a.png','/photos/b.png']);assert.equal(p.get('exportPendingRetry').hidden,true);
  });
  await test('late export result review cannot replace a newly opened single-photo editor',async()=>{
    const result=deferred();const p=await exportPage(path=>path.startsWith('/api/export?')?result.promise:undefined);const opening=p.run('openExportResults("done")');p.run('selectedPaths=new Set([library[0].path]);');await p.run('process("polish")');result.resolve({job:{...exportJob('done','completed'),items:[],items_total:0}});await opening;assert.equal(p.run('CURRENT_EDITOR.path'),'/photos/a.png');assert.equal(p.get('results').classList.contains('single-result'),true);
  });
  process.exitCode=failures?1:0;
}
main().catch(e=>{console.error(e);process.exitCode=1;});
