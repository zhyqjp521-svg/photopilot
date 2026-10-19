// Isolated browser fixture: no requests reach the user's running app or files.
const fs=require('node:fs'),path=require('node:path'),assert=require('node:assert/strict');
const {chromium}=process.env.PLAYWRIGHT_CORE?(require('playwright-core')):(require('playwright'));
async function main(){
  const source=fs.readFileSync(path.join(__dirname,'../photopilot/server.py'),'utf8');
  const html=source.split('INDEX_HTML = r"""')[1].split('</html>')[0]+'</html>';
  const browser=await chromium.launch({headless:true,channel:process.platform==='win32'?'msedge':undefined,
    executablePath:process.platform==='win32'?undefined:'/Applications/Google Chrome.app/Contents/MacOS/Google Chrome'});
  const errors=[],writes=[];let saved=null;
  try{
    const page=await browser.newPage({viewport:{width:1440,height:1000}});
    page.on('pageerror',error=>errors.push(error.message));
    await page.route('**/*',async route=>{
      const request=route.request(),url=new URL(request.url()),body=request.postDataJSON();
      if(url.pathname==='/')return route.fulfill({contentType:'text/html',body:html.replace('__INIT_FOLDER_JSON__','""')});
      if(url.pathname==='/qa/thumb.svg')return route.fulfill({contentType:'image/svg+xml',body:'<svg xmlns="http://www.w3.org/2000/svg" width="900" height="600"><rect width="900" height="600" fill="#45647a"/></svg>'});
      if(body)writes.push({route:url.pathname,body});
      let data={};
      if(url.pathname==='/api/presets')data={presets:[{name:'natural'}]};
      if(url.pathname==='/api/exports')data={jobs:[],queue_error:null};
      if(url.pathname==='/api/project'){if(body)saved=body;data=body?{ok:true}:{state:saved,missing:[],changed:[]};}
      if(url.pathname==='/api/rate')data={ok:true,rating:body.rating};
      await route.fulfill({contentType:'application/json',body:JSON.stringify(data)});
    });
    await page.goto('http://photopilot-size-qa.local/');
    await page.waitForFunction(()=>PROJECT.ready);
    await page.evaluate(()=>{
      const fixture=(path,w,h,bytes)=>({path,thumb:'/qa/thumb.svg',width:1024,height:683,original_width:w,original_height:h,file_size:bytes,score:.8,flags:[],rating:0});
      const photos=[fixture('/qa/icon.png',64,64,300),fixture('/qa/a.jpg',3000,2000,1048576),fixture('/qa/b.jpg',2000,3000,2097152),fixture('/qa/small.jpg',900,600,50000)];
      replaceProjectLibrary(photos);PROJECT.folder='/qa';$('folder').value='/qa';selectedPaths=new Set(photos.map(p=>p.path));
      $('empty').style.display='none';rebuildView();
    });
    await page.locator('#reviewTools summary').first().click();
    await page.locator('#sizeFilters summary').click();
    for(const [id,value] of Object.entries({sizeMinEdge:'1500',sizeMaxEdge:'2500',sizeMinMB:'1',sizeMaxMB:'2'}))await page.locator('#'+id).fill(value);
    await page.getByRole('button',{name:'应用范围',exact:true}).click();
    assert.deepEqual(await page.evaluate(()=>ranked.map(p=>p.path)),['/qa/a.jpg','/qa/b.jpg']);
    assert.equal(await page.locator('#grid .card').count(),2);
    assert.deepEqual(await page.evaluate(()=>selectedPhotos().map(p=>p.path)),['/qa/a.jpg','/qa/b.jpg']);
    await page.locator('#sizeMaxEdge').fill('100');
    await page.getByRole('button',{name:'应用范围',exact:true}).click();
    assert.match(await page.locator('#sizeFilterStatus').textContent(),/下限不能大于上限/);
    assert.equal(await page.locator('#grid .card').count(),2);
    await page.locator('#sizeMaxEdge').fill('2500');
    await page.getByRole('button',{name:'应用范围',exact:true}).click();
    await page.evaluate(()=>saveProject(true));await page.reload();await page.waitForFunction(()=>PROJECT.ready&&library.length===4);
    assert.equal(await page.locator('#sizeMinEdge').inputValue(),'1500');
    assert.equal(await page.locator('#grid .card').count(),2);
    await page.locator('#reviewTools summary').first().click();
    await page.locator('#sizeFilters summary').click();
    const out=process.env.QA_OUT||'/tmp';
    await page.screenshot({path:path.join(out,'photopilot-size-filter-wide.png')});
    await page.setViewportSize({width:1080,height:800});
    await page.screenshot({path:path.join(out,'photopilot-size-filter-compact.png')});
    assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth),false);
    await page.selectOption('#filterMode','groups');
    await page.locator('#filterEmpty').waitFor({state:'visible'});
    await page.getByRole('button',{name:'清除筛选，显示全部',exact:true}).click();
    assert.equal(await page.locator('#grid .card').count(),4);
    await page.getByRole('button',{name:'排除小图（短边 ≥600 px）',exact:true}).click();
    assert.equal(await page.locator('#grid .card').count(),3);
    assert.equal(writes.some(item=>item.route==='/api/trash'||item.route==='/api/rate'),false);
    assert.deepEqual(errors,[]);
    console.log('PASS real-browser size ranges, invalid input, persistence, empty recovery, small-image preset, 1440/1080 layouts, no photo writes');
  }finally{await browser.close();}
}
main().catch(error=>{console.error(error);process.exitCode=1;});
