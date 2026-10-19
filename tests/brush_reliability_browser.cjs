// Isolated real-browser QA. No API writes reach user data or a live app.
const fs=require('node:fs'),path=require('node:path'),assert=require('node:assert/strict');
const {chromium}=require('playwright');
async function main(){
  const source=fs.readFileSync(path.join(__dirname,'../photopilot/server.py'),'utf8');
  const html=source.split('INDEX_HTML = r"""')[1].split('</html>')[0]+'</html>';
  const browser=await chromium.launch({headless:true,executablePath:'/Applications/Google Chrome.app/Contents/MacOS/Google Chrome'});
  const requests=[],errors=[];let sequence=0,holdExport=false,releaseExport;
  try{
    const page=await browser.newPage({viewport:{width:1440,height:1000}});
    page.on('pageerror',e=>errors.push(e.message));
    await page.route('**/*',async route=>{
      const url=new URL(route.request().url()),body=route.request().postDataJSON();
      if(url.pathname==='/')return route.fulfill({contentType:'text/html',body:html.replace('__INIT_FOLDER_JSON__','""')});
      if(url.pathname.startsWith('/qa/'))return route.fulfill({contentType:'image/svg+xml',body:'<svg xmlns="http://www.w3.org/2000/svg" width="1200" height="800"><rect width="1200" height="800" fill="#547d92"/></svg>'});
      if(body)requests.push({path:url.pathname,body});
      let data={};
      if(url.pathname==='/api/presets')data={presets:[{name:'natural'}]};
      if(url.pathname==='/api/project')data={state:null,ok:true};
      if(url.pathname==='/api/exports')data={jobs:[]};
      if(url.pathname==='/api/prepare'){
        if(holdExport)await new Promise(resolve=>releaseExport=resolve);
        data={sid:'brush-session-'+ ++sequence,count:1,ref_desc:'隔离测试'};
      }
      if(url.pathname==='/api/process_one')data={before:'/qa/original.svg',after:'/qa/after.svg?v='+ ++sequence,out:'/qa/export.png',width:1200,height:800};
      await route.fulfill({contentType:'application/json',body:JSON.stringify(data)});
    });
    await page.goto('http://photopilot-brush.local/');
    await page.waitForFunction(()=>document.querySelector('#presetSelect').options.length>0);
    await page.evaluate(()=>{
      const p={path:'/qa/photo.png',thumb:'/qa/thumb.svg',width:1200,height:800,score:.8,flags:[],rating:0};
      library=[p];libraryByPath.set(p.path,p);ranked=library;selectedPaths=new Set([p.path]);
      $('folder').value='/qa';PROJECT.applying=true;EXPORT_QUEUE.ready=true;
    });
    await page.evaluate(()=>process('polish'));
    await page.waitForFunction(()=>document.querySelector('#editStage img.b')?.naturalWidth===1200);
    await page.evaluate(()=>selectEditorPanel('portrait'));
    await page.selectOption('#editRegion','brush');
    const canvas=page.locator('#editBrushCanvas');await canvas.waitFor({state:'visible'});
    const box=await canvas.boundingBox();assert.ok(box.width>100&&box.height>100);
    await page.mouse.move(box.x+box.width*.2,box.y+box.height*.3);await page.mouse.down();
    await page.mouse.move(box.x+box.width*.65,box.y+box.height*.6,{steps:12});await page.mouse.up();
    await page.waitForFunction(()=>!!CURRENT_EDITOR.retouch.brush_mask);
    const painted=await page.evaluate(()=>CURRENT_EDITOR.retouch.brush_mask);
    const coverage=await canvas.evaluate(c=>{const a=c.getContext('2d').getImageData(0,0,c.width,c.height).data;let soft=0,empty=0;for(let i=3;i<a.length;i+=4){if(a[i]===0)empty++;else if(a[i]<255)soft++;}return{soft,empty};});
    assert.ok(coverage.soft>0&&coverage.empty>0,'real pointer painting retains feather alpha and untouched area');
    await page.evaluate(()=>undoEditorTone());assert.equal(await page.evaluate(()=>CURRENT_EDITOR.retouch.brush_mask),null);
    await page.evaluate(()=>redoEditorTone());assert.equal(await page.evaluate(()=>CURRENT_EDITOR.retouch.brush_mask),painted);
    await page.evaluate(()=>closeResults());await page.evaluate(()=>process('polish'));
    assert.equal(await page.evaluate(()=>CURRENT_EDITOR.brushMask),painted,'reopen restores canvas mask state');
    await page.evaluate(()=>{const n=$('edit-retouch-clarity');n.value='.6';n.dispatchEvent(new Event('input'));});
    assert.equal(await page.evaluate(()=>CURRENT_EDITOR.retouch.brush_mask),painted,'slider preserves reopened mask');
    await page.evaluate(()=>flushEditorGesture());
    await page.waitForFunction(()=>!$('editBrushCanvas')._maskLoading);
    const reopened=await canvas.boundingBox();
    await page.mouse.move(reopened.x+reopened.width*.3,reopened.y+reopened.height*.8);await page.mouse.down();
    await page.mouse.move(reopened.x+reopened.width*.7,reopened.y+reopened.height*.8,{steps:8});await page.mouse.up();
    await page.waitForFunction(previous=>CURRENT_EDITOR.retouch.brush_mask!==previous,painted);
    await page.evaluate(()=>undoEditorTone());
    assert.equal(await page.evaluate(()=>CURRENT_EDITOR.retouch.brush_mask),painted,'painting after reopen is committed and undoable');
    holdExport=true;await page.locator('#editExport').click();
    await page.waitForFunction(()=>$('editBrushClear').disabled);
    await page.evaluate(()=>clearBrushMask());assert.equal(await page.evaluate(()=>CURRENT_EDITOR.retouch.brush_mask),painted);
    assert.ok(releaseExport);holdExport=false;releaseExport();
    await page.waitForFunction(()=>!CURRENT_EDITOR.exporting);
    const exported=requests.filter(r=>r.path==='/api/process_one').at(-1).body;
    assert.equal(exported.retouch_adjustments.brush_mask,painted,'export carries the same frozen mask');
    assert.equal(exported.retouch_adjustments.clarity,.6);
    assert.equal(await page.locator('#editBrushClear').isDisabled(),false);
    await page.evaluate(()=>clearBrushMask());assert.equal(await page.evaluate(()=>CURRENT_EDITOR.retouch.brush_mask),null);
    assert.deepEqual(errors,[]);
    console.log('PASS real pointer paint/feather, undo/redo, close/reopen, slider preservation, frozen export mask, export lock and clear; isolated API writes only');
  }finally{await browser.close();}
}
main().catch(e=>{console.error(e);process.exitCode=1;});
