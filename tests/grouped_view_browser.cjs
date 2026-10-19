// Optional rendered QA: NODE_PATH=<runtime node_modules> node tests/grouped_view_browser.cjs
// All write endpoints are intercepted; real photo files are never modified.
const assert=require('node:assert/strict');
const fs=require('node:fs');
const os=require('node:os');
const path=require('node:path');
const {chromium}=process.env.PLAYWRIGHT_CORE?(require('playwright-core')):(require('playwright'));
async function main(){
  const source=fs.readFileSync(path.join(__dirname,'../photopilot/server.py'),'utf8');
  const html=source.split('INDEX_HTML = r"""')[1].split('</html>')[0]+'</html>';
  let originals,thumbnails;
  try{
    const checkpoint=await (await fetch('http://127.0.0.1:8618/api/project',{signal:AbortSignal.timeout(3000)})).json();
    originals=checkpoint.state.photos.slice(0,12);
    assert.equal(originals.length,12);
    thumbnails=await Promise.all(originals.map(async p=>Buffer.from(await (await fetch('http://127.0.0.1:8618'+p.thumb)).arrayBuffer())));
  }catch{
    originals=Array.from({length:12},(_,i)=>({path:'/qa/'+i+'.jpg',faces:1,blinks:0,ai:null,width:100,height:100}));
    const png=Buffer.from('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jRZkAAAAASUVORK5CYII=','base64');
    thumbnails=originals.map(()=>png);
    console.log('Using offline image fixtures; no live project was accessed');
  }
  const fixture=originals.map((p,i)=>({...p,path:i===0?'/qa/婚礼现场_新郎新娘合影_非常长的文件名称_DSC01234.ARW':'/qa/photo-'+i+'.jpg',thumb:'/qa/thumb/'+i,
    flags:[],score:.9-i*.02,rating:0,group:i<3?1:i<5?2:i<9?3:10+i,
    group_size:i<3?3:i<5?2:i<9?4:1,group_rank:i<3?i+1:i<5?i-2:i<9?i-4:1,
    group_best:[0,3,5,9,10,11].includes(i)}));
  const browser=await chromium.launch({headless:true,channel:process.platform==='win32'?'msedge':undefined,
    executablePath:process.platform==='win32'?undefined:'/Applications/Google Chrome.app/Contents/MacOS/Google Chrome'});
  const output=fs.mkdtempSync(path.join(os.tmpdir(),'photopilot-groups-'));
  try{
    const page=await browser.newPage({viewport:{width:1440,height:920}});
    const errors=[],writes=[];page.on('pageerror',e=>errors.push(e.message));
    await page.route('**/*',async route=>{
      const url=new URL(route.request().url());
      if(route.request().method()==='POST')writes.push(url.pathname);
      if(url.pathname==='/')return route.fulfill({contentType:'text/html',body:html.replace('__INIT_FOLDER_JSON__','""').replace('__INIT_PLACEHOLDER__','测试照片（隔离测试）')});
      if(url.pathname.startsWith('/qa/thumb/'))return route.fulfill({contentType:'image/jpeg',body:thumbnails[Number(url.pathname.split('/').pop())]});
      if(url.pathname==='/api/raw_img')return route.fulfill({contentType:'image/svg+xml',body:'<svg xmlns="http://www.w3.org/2000/svg" width="2000" height="1000"><rect width="2000" height="1000" fill="#789"/></svg>'});
      const response=url.pathname==='/api/presets'?{presets:[{name:'natural'}]}:url.pathname==='/api/exports'?{jobs:[]}:
        url.pathname==='/api/project'?{state:null}:url.pathname==='/api/rate'?{ok:true}:{};
      return route.fulfill({contentType:'application/json',body:JSON.stringify(response)});
    });
    await page.goto('http://photopilot-qa.local/');
    await page.evaluate(photos=>{library=photos;library.forEach(p=>libraryByPath.set(p.path,p));PROJECT.applying=true;
      sortMode='smart';filterMode='all';$('empty').style.display='none';rebuildView();},fixture);
    await page.locator('.th.ld').first().waitFor();
    assert.equal(await page.locator('.photoGroup').count(),4);
    assert.deepEqual(await page.locator('.groupPhotos').evaluateAll(nodes=>nodes.map(n=>n.children.length)),[3,2,4,3]);
    const sections=page.locator('.photoGroup');
    await sections.nth(0).getByRole('button',{name:'整组选中',exact:true}).click();
    assert.match(await sections.nth(0).locator('.groupSummary').innerText(),/已选 3/);
    assert.equal(await page.locator('.card.sel').count(),3);
    await page.keyboard.press('2');
    assert.deepEqual(await page.evaluate(()=>library.slice(0,3).map(p=>p.rating)),[2,2,2]);
    await page.waitForFunction(()=>!BATCH_BUSY);
    await page.keyboard.press('0');
    await page.waitForFunction(()=>!BATCH_BUSY);
    assert.deepEqual(await page.evaluate(()=>library.slice(0,3).map(p=>p.rating)),[0,0,0]);
    await page.setViewportSize({width:1440,height:1600});
    await page.evaluate(()=>{$('grid').scrollTop=0;activeGroupKey='group:1';syncGroupNavigation();});
    await page.locator('#nextGroup').click();
    await page.waitForTimeout(100);
    assert.equal(await page.evaluate(()=>activeGroupKey),'group:2');
    await page.locator('#nextGroup').click();
    await page.waitForTimeout(100);
    assert.equal(await page.evaluate(()=>activeGroupKey),'group:3','short neighboring groups must not skip the navigation destination');
    await page.locator('#previousGroup').click();
    await page.locator('#previousGroup').click();
    await page.setViewportSize({width:1440,height:920});
    await page.locator('#nextGroup').click();
    await page.evaluate(()=>$('grid').scrollTop=0);
    await page.waitForFunction(()=>activeGroupKey==='group:1'&&groupJumpTop===null);
    await page.getByRole('button',{name:'全部收起',exact:true}).click();
    assert.equal(await page.locator('.groupPhotos:visible').count(),0);
    await page.getByRole('button',{name:'全部展开',exact:true}).click();
    assert.equal(await page.locator('.groupPhotos:visible').count(),4);
    await sections.nth(0).locator('.card').first().click();
    assert.match(await sections.nth(0).locator('.groupSummary').innerText(),/已选 2/);
    await page.evaluate(()=>window.qaImage=document.querySelector('.groupPhotos img'));
    await sections.nth(0).getByRole('button',{name:'收起',exact:true}).click();
    assert.equal(await sections.nth(0).locator('.groupPhotos').isVisible(),false);
    await sections.nth(0).getByRole('button',{name:'展开',exact:true}).click();
    assert.equal(await page.evaluate(()=>qaImage===document.querySelector('.groupPhotos img')),true);
    await sections.nth(0).getByRole('button',{name:'组内对比',exact:true}).click();
    assert.equal(await page.locator('#compareModal').evaluate(n=>n.classList.contains('show')),true);
    assert.equal(await page.locator('.comparePane').count(),3);
    await page.keyboard.press('4');
    await page.waitForFunction(()=>!pendingRatings.size);
    assert.deepEqual(await page.evaluate(()=>library.slice(0,3).map(p=>p.rating)),[4,0,0],'comparison keys rate the active pane only');
    await page.keyboard.press('0');await page.waitForFunction(()=>!pendingRatings.size);
    await page.locator('.comparePane').nth(1).click();await page.keyboard.press('3');
    await page.waitForFunction(()=>!pendingRatings.size);
    assert.deepEqual(await page.evaluate(()=>library.slice(0,3).map(p=>p.rating)),[0,3,0]);
    await page.keyboard.press('0');await page.waitForFunction(()=>!pendingRatings.size);
    await page.locator('#compareReject').click();assert.equal(await page.evaluate(()=>selectedPaths.has(library[1].path)),false);
    await page.locator('#compareKeep').click();assert.equal(await page.evaluate(()=>selectedPaths.has(library[1].path)),true);
    await page.locator('.comparePane img').nth(1).waitFor();
    await page.waitForFunction(()=>document.querySelectorAll('.comparePane img')[1].naturalWidth===2000);
    await page.getByRole('button',{name:'100% 预览像素',exact:true}).click();
    assert.ok(await page.locator('.comparePane img').nth(1).evaluate(img=>Math.abs(img.getBoundingClientRect().width*devicePixelRatio-img.naturalWidth)<1));
    await page.evaluate(()=>closeGroupCompare());
    await sections.nth(0).locator('.starBtn').first().click();
    await sections.nth(0).locator('.ratingMenu [data-rating="3"]').first().click();
    assert.equal(await page.evaluate(()=>library[0].rating),3);
    await page.screenshot({path:path.join(output,'wide.png')});
    await page.selectOption('#filterMode','starred');
    assert.match(await page.locator('#scanInfo').innerText(),/当前可见已选 0.*全部已选 2/);
    await page.locator('#reviewTools summary').first().click();
    assert.match(await page.locator('#batchSelectionCount').innerText(),/全部已选 2.*当前可见 0.*隐藏 2/);
    const confirmations=[];page.on('dialog',dialog=>{confirmations.push(dialog.message());dialog.dismiss();});
    const before=writes.length;
    await page.locator('#projectRemoveBtn').click();await page.locator('#trashBtn').click();
    await page.selectOption('#batchRating','2');await page.locator('#batchRateBtn').click();
    assert.equal(confirmations.length,3);
    for(const message of confirmations)assert.match(message,/隐藏 2 张.*隐藏照片也包含在本次操作中/);
    assert.equal(writes.length,before,'canceling a hidden-photo confirmation must not write any files or project');
    await page.locator('#clearHiddenSelection').click();assert.equal(await page.evaluate(()=>selectedPaths.size),0);
    await page.selectOption('#filterMode','all');
    for(const width of [1080,560]){
      await page.setViewportSize({width,height:760});
      const clipped=await page.locator('.photoGroup').evaluateAll(nodes=>nodes.some(n=>n.scrollWidth>n.clientWidth+1));
      assert.equal(clipped,false,`group content must not overflow at ${width}px`);
      await page.evaluate(()=>$('grid').scrollTop=0);
      await page.screenshot({path:path.join(output,'width-'+width+'.png')});
    }
    await page.setViewportSize({width:1440,height:920});
    await page.evaluate(()=>{const p=library[0];p.flags=['过暗','模糊','连拍重复','闭眼'];updateCardInPlace(cardCache.get(p.path),p);});
    for(const density of ['compact','comfortable','large']){
      await page.evaluate(d=>setDensity(d,false),density);
      const layout=await page.locator('.card').first().evaluate(card=>{
        const name=card.querySelector('.name').getBoundingClientRect(),flags=card.querySelector('.flags').getBoundingClientRect(),badges=card.querySelector('.badges').getBoundingClientRect();
        const overlaps=(a,b)=>a.left<b.right&&a.right>b.left&&a.top<b.bottom&&a.bottom>b.top;
        return {overlap:overlaps(name,flags)||overlaps(name,badges)||overlaps(flags,badges),clipped:card.scrollHeight>card.clientHeight+1,title:card.querySelector('.name').title};
      });
      assert.equal(layout.overlap,false,`filename and labels overlap at ${density} density`);
      assert.equal(layout.clipped,false,`metadata clipped at ${density} density`);
      assert.match(layout.title,/DSC01234.ARW/);
    }
    await page.evaluate(()=>setDensity('comfortable',false));
    await page.evaluate(()=>$('grid').scrollTop=0);
    await page.screenshot({path:path.join(output,'metadata.png')});
    await page.evaluate(photos=>{library=Array.from({length:245},(_,i)=>({...photos[i%12],path:'/qa/large-'+i+'.jpg',group:i<242?1:2,group_size:i<242?242:3}));
      renderedN=0;rebuildView();},fixture);
    await page.locator('#nextGroup').click();
    await page.waitForTimeout(100);
    assert.equal(await page.evaluate(()=>activeGroupKey),'group:2','navigation must retain its destination at the bottom');
    assert.deepEqual(await page.locator('.groupPhotos').evaluateAll(nodes=>nodes.map(n=>n.children.length)),[242,3]);
    await page.evaluate(()=>{renderedN=0;rebuildView();setAllGroupsCollapsed(true);});
    await page.waitForTimeout(100);
    assert.equal(await page.evaluate(()=>renderedN),240,'folded overview must not eagerly construct hidden photo cards');
    await page.locator('#nextGroup').click();
    await page.getByRole('button',{name:'全部展开',exact:true}).click();
    await page.waitForFunction(()=>renderedN===245);
    assert.deepEqual(await page.locator('.groupPhotos').evaluateAll(nodes=>nodes.map(n=>n.children.length)),[242,3]);
    await page.selectOption('#sortMode','score');assert.equal(await page.locator('.photoGroup').count(),0);
    await page.selectOption('#filterMode','groups');assert.equal(await page.locator('.photoGroup').count(),2);
    await page.evaluate(photos=>{
      library=Array.from({length:900},(_,i)=>({...photos[i%12],path:'/qa/many-'+i+'.jpg',group:Math.floor(i/3)+1,group_size:3}));
      collapsedGroups.clear();activeGroupKey=null;renderedN=0;
      $('grid').scrollTop=0;rebuildView();setAllGroupsCollapsed(true);
    },fixture);
    assert.equal(await page.locator('.photoGroup').count(),300,'collapsed overview contains every group heading');
    await page.getByRole('button',{name:'全部展开',exact:true}).click();
    assert.equal(await page.locator('.groupPhotos').evaluateAll(nodes=>nodes.some(n=>!n.hidden&&n.children.length===0)),false,
      'expanding a large overview must not leave empty photo groups');
    assert.equal(await page.evaluate(()=>renderedN),240,'unfolding must retain incremental loading');
    await page.screenshot({path:path.join(output,'large-overview-expanded.png')});
    await page.evaluate(()=>{$('grid').scrollTop=$('grid').scrollHeight;});
    await page.waitForFunction(()=>renderedN>240);
    assert.equal(await page.locator('.groupPhotos').evaluateAll(nodes=>nodes.some(n=>!n.hidden&&n.children.length===0)),false,
      'scrolling after unfold continues real photo groups without blank headings');
    const largeBefore=await page.evaluate(photos=>{
      library=Array.from({length:6000},(_,i)=>({...photos[i%12],path:'/qa/window-'+i+'.jpg',group:Math.floor(i/3)+1,group_size:3}));
      libraryByPath.clear();library.forEach(p=>libraryByPath.set(p.path,p));cardCache.clear();collapsedGroups.clear();
      selectedPaths=new Set();renderedN=0;activeGroupKey=null;$('grid').scrollTop=0;
      const start=performance.now();rebuildView();window.qaDestination=VIEW_GROUPS[VIEW_GROUPS.length-1];
      selectedPaths.add(qaDestination.members[0].path);activeGroupKey=VIEW_GROUPS[VIEW_GROUPS.length-2].key;navigateGroup(1);
      return {ms:performance.now()-start,cards:document.querySelectorAll('#grid .card').length,cache:cardCache.size};
    },fixture);
    console.log('6000-photo distant-group navigation:',largeBefore);
    assert.ok(largeBefore.cards<=480,'distant navigation must not construct preceding thousands of cards');
    assert.ok(largeBefore.cache<=720,'offscreen card cache must be bounded');
    await page.waitForTimeout(100);
    assert.equal(await page.evaluate(()=>activeGroupKey===qaDestination.key),true);
    assert.equal(await page.evaluate(()=>cardCache.get(qaDestination.members[0].path).classList.contains('sel')),true);
    await page.locator('#previousGroup').click();
    assert.equal(await page.evaluate(()=>selectedPaths.has(qaDestination.members[0].path)),true);
    await page.evaluate(()=>$('grid').scrollTop=0);await page.waitForTimeout(100);
    await page.evaluate(()=>{window.qaTargetA=ranked[0].path;setTarget(qaTargetA);});
    await page.evaluate(()=>$('grid').scrollTop=$('grid').scrollHeight);await page.waitForTimeout(100);
    assert.equal(await page.evaluate(()=>activeGroupKey===VIEW_GROUPS.at(-1).key),true,'manual bottom scroll identifies the final short virtual group');
    await page.evaluate(()=>{setTarget(ranked.at(-1).path);$('grid').scrollTop=0;});await page.waitForTimeout(100);
    assert.equal(await page.evaluate(()=>cardCache.get(qaTargetA).classList.contains('target')),false,'remounting a cached card clears the previous color target');
    await page.evaluate(()=>{setAllGroupsCollapsed(true);activeGroupKey=VIEW_GROUPS[VIEW_GROUPS.length-2].key;navigateGroup(1);});
    assert.ok(await page.evaluate(()=>groupSections.get(activeGroupKey).body.children.length)>0,'jumping from collapsed overview loads its destination');
    for(const density of ['compact','large','comfortable']){
      await page.evaluate(d=>setDensity(d,false),density);
      const sizing=await page.evaluate(()=>{
        const entry=groupSections.get(activeGroupKey),metrics=virtualGroupMetrics();
        return {expected:metrics.height,actual:parseFloat(entry.body.style.gridAutoRows)};
      });
      assert.equal(sizing.actual,sizing.expected,'virtual rows follow thumbnail density');
    }
    await page.selectOption('#sortMode','score');await page.selectOption('#filterMode','all');
    await page.evaluate(()=>$('grid').scrollTop=$('grid').scrollHeight);
    await page.waitForTimeout(100);
    assert.equal(await page.evaluate(()=>!!document.querySelector('#grid .card[data-path="'+ranked.at(-1).path+'"]')),true,
      'flat large-library grid can scroll directly to the last photo');
    assert.ok(await page.locator('#grid .card').count()<=480,'flat grid photo DOM is bounded too');
    await page.evaluate(()=>$('grid').scrollTop=virtualGroupMetrics().row*20+100);
    await page.waitForTimeout(100);
    const mutations=await page.evaluate(async()=>{
      let count=0;const observer=new MutationObserver(records=>count+=records.length);
      observer.observe($('grid'),{childList:true});$('grid').scrollTop+=5;
      await new Promise(resolve=>setTimeout(resolve,100));observer.disconnect();return count;
    });
    assert.equal(mutations,0,'scrolling within the same virtual rows preserves card DOM');
    await page.setViewportSize({width:1080,height:800});
    await page.waitForTimeout(100);
    await page.evaluate(()=>$('grid').scrollTop=$('grid').scrollHeight);
    await page.waitForTimeout(100);
    assert.equal(await page.evaluate(()=>!!cardCache.get(ranked.at(-1).path)?.isConnected),true,'resized flat grid reaches its last row');
    await page.evaluate(()=>{library.forEach(p=>{p.group=1;p.group_size=library.length;});collapsedGroups.clear();sortMode='smart';rebuildView();$('grid').scrollTop=$('grid').scrollHeight;});
    await page.waitForTimeout(100);
    assert.equal(await page.evaluate(()=>!!cardCache.get(ranked.at(-1).path)?.isConnected),true,'one giant group also virtualizes its rows');
    assert.ok(await page.locator('#grid .card').count()<=480);
    await page.evaluate(()=>{CULL_HISTORY.undo=[];CULL_HISTORY.redo=[];selectedPaths.clear();selAll(1);});
    await page.keyboard.press('Meta+z');
    assert.equal(await page.evaluate(()=>selectedPaths.size),0,'native shortcut undoes bulk selection');
    await page.keyboard.press('Meta+Shift+z');
    assert.equal(await page.evaluate(()=>selectedPaths.size),6000,'native shortcut redoes bulk selection');
    assert.deepEqual(errors,[]);
    console.log('PASS rendered groups, selection, group navigation including neighboring short groups and manual scrolling, fold/unfold all without eager hidden cards or blank groups across 900 photos / 300 groups, keyboard 0–5 ratings and comparison conflict protection, image reuse, 3-photo comparison, 3-star rating, hidden-selection counts and canceled confirmations, metadata geometry at all densities, responsive layout, 245-photo lazy chunks and mode switches');
    console.log('Screenshots:',output);
  }finally{await browser.close();}
}
main().catch(e=>{console.error(e);process.exitCode=1;});
