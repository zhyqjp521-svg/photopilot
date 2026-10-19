// All API writes are intercepted. No access to the live app or real photo files.
const fs=require('node:fs'),path=require('node:path'),assert=require('node:assert/strict');
const {chromium}=process.env.PLAYWRIGHT_CORE?(require('playwright-core')):(require('playwright'));
async function main(){
  const source=fs.readFileSync(path.join(__dirname,'../photopilot/server.py'),'utf8');
  const html=source.split('INDEX_HTML = r"""')[1].split('</html>')[0]+'</html>';
  const browser=await chromium.launch({headless:true,channel:process.platform==='win32'?'msedge':undefined,
    executablePath:process.platform==='win32'?undefined:'/Applications/Google Chrome.app/Contents/MacOS/Google Chrome'});
  try{
    let releaseFullPreview;
    const fullPreviewReady=new Promise(resolve=>{releaseFullPreview=resolve;});
    const page=await browser.newPage({viewport:{width:1440,height:900}}),errors=[],writes=[];
    page.on('pageerror',e=>errors.push(e.message));
    await page.route('**/*',async route=>{
      const request=route.request(),u=new URL(request.url()),body=request.postDataJSON();
      if(u.pathname==='/')return route.fulfill({contentType:'text/html',body:html.replace('__INIT_FOLDER_JSON__','""')});
      if(u.pathname==='/qa/thumb.svg'||u.pathname==='/api/raw_img'){
        if(u.pathname==='/api/raw_img')await fullPreviewReady;
        return route.fulfill({contentType:'image/svg+xml',body:'<svg xmlns="http://www.w3.org/2000/svg" width="1200" height="800"><rect width="1200" height="800" fill="#547d92"/></svg>'});
      }
      if(body)writes.push(u.pathname);
      let data={};
      if(u.pathname==='/api/presets')data={presets:[{name:'natural'}]};
      if(u.pathname==='/api/project')data=body?{ok:true}:{state:null};
      if(u.pathname==='/api/exports')data={jobs:[]};
      if(u.pathname==='/api/regroup')data={job:'group-1'};
      if(u.pathname==='/api/regroup_status')data={finished:true,done:4,total:4,skipped:0,photos:[0,1,2,3].map(i=>({path:'/qa/'+i+'.jpg',group:i<2?0:i,group_size:i<2?2:1,group_rank:i===1?2:1,group_best:i!==1}))};
      await route.fulfill({contentType:'application/json',body:JSON.stringify(data)});
    });
    await page.goto('http://photopilot-review-qa.local/');await page.waitForFunction(()=>PROJECT.ready);
    await page.evaluate(()=>{
      const photos=Array.from({length:4},(_,i)=>({path:'/qa/'+i+'.jpg',thumb:'/qa/thumb.svg',width:1200,height:800,original_width:3000,original_height:2000,file_size:2000000,score:.9-i*.05,rating:i,flags:[],group:-1,group_size:1,group_rank:1,group_best:false}));
      replaceProjectLibrary(photos);PROJECT.folder='/qa';PROJECT.applying=true;$('folder').value='/qa';$('empty').style.display='none';rebuildView();
    });
    await page.locator('#grid .th.ld').first().waitFor();
    const geometry=await page.evaluate(()=>({gridTop:$('grid').getBoundingClientRect().top,gridHeight:$('grid').clientHeight,gridWidth:$('grid').clientWidth,viewportWidth:innerWidth,sideVisible:!$('side').hidden}));
    console.log('workspace geometry',JSON.stringify(geometry));
    assert.ok(geometry.gridTop<=145,'compact utility row should bring the grid above 145px');
    assert.ok(geometry.gridHeight>=600,'at least 600px of grid height at 900px window');
    assert.equal(geometry.sideVisible,false);
    const card=page.locator('#grid .card').nth(1);await card.click();await page.keyboard.press('Space');
    await page.locator('#lb.show').waitFor();
    assert.match(await page.locator('#lbName').innerText(),/1.jpg/);
    await page.waitForFunction(()=>document.querySelector('#lbImg').naturalWidth>0);
    assert.ok(await page.evaluate(()=>$('lbImg').src.includes('/qa/thumb.svg')),'thumbnail visible while large preview is delayed');
    releaseFullPreview();
    const selectedBefore=await page.evaluate(()=>[...selectedPaths]);
    await page.keyboard.press('Space');await page.locator('#lb.show').waitFor({state:'hidden'});
    assert.deepEqual(await page.evaluate(()=>[...selectedPaths]),selectedBefore,'Space return must not toggle keep selection');
    await page.keyboard.press('Space');await page.locator('#lb.show').waitFor();
    await page.keyboard.press('Escape');
    assert.equal(await page.evaluate(()=>document.activeElement?.dataset.path),'/qa/1.jpg');
    await page.locator('#sideToggle').click();await page.locator('#side').waitFor({state:'visible'});
    await page.locator('#sideToggle').click();
    await page.locator('#regroupButton').click();
    await page.waitForFunction(()=>!REGROUP.busy&&library[0].group_size===2);
    assert.deepEqual(await page.evaluate(()=>library.map(p=>p.rating)),[0,1,2,3]);
    assert.deepEqual(await page.evaluate(()=>[...selectedPaths]),['/qa/1.jpg']);
    await page.selectOption('#filterMode','groups');assert.equal(await page.locator('#grid .card').count(),2);
    const firstPhotoTop=await page.locator('.groupPhotos .th').first().evaluate(n=>n.getBoundingClientRect().top);
    assert.ok(firstPhotoTop<=215,`group photos should begin above 215px, got ${firstPhotoTop}`);
    await page.evaluate(()=>$('grid').scrollTop=40);
    assert.equal(await page.locator('.groupHeader').first().evaluate(n=>getComputedStyle(n).position),'static','group headings must not float over photo content');
    await page.evaluate(()=>$('grid').scrollTop=0);
    await page.screenshot({path:'/tmp/photopilot-review-wide.png'});
    await page.setViewportSize({width:1080,height:680});
    assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth),false);
    assert.ok(await page.locator('#grid').evaluate(n=>n.clientHeight)>350);
    await page.screenshot({path:'/tmp/photopilot-review-compact.png'});
    await page.locator('#reviewTools summary').first().click();
    await page.locator('#sizeFilters summary').click();
    await page.locator('#sizeMinMB').fill('0.3');await page.getByRole('button',{name:'应用范围',exact:true}).click();
    assert.equal(await page.locator('#grid .card').count(),2);
    assert.deepEqual(errors,[]);
    assert.equal(writes.some(p=>p==='/api/trash'||p==='/api/rate'),false);
    console.log('PASS compact review layout, Space quicklook, progressive preview, focus return, panel toggle, regroup, preserved ratings/selection, size controls');
  }finally{await browser.close();}
}
main().catch(error=>{console.error(error);process.exitCode=1;});
