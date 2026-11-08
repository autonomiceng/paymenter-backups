# MIT, Copyright (c) 2026 Autonomic Engineering.
"""Render native diagnostics with pinned Paymenter; check command visibility in a narrow rail."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tarfile
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
PHP = r'''<?php
$base=$argv[1];$root=$argv[2];
$loader=require $base.'/vendor/autoload.php';
$loader->addPsr4('App\\',$base.'/app/',true);
$loader->addPsr4('Paymenter\\Extensions\\Others\\Backups\\',$root.'/extension/Backups/',true);
$app=require $base.'/bootstrap/app.php';$app->make(Illuminate\Contracts\Console\Kernel::class)->bootstrap();
Illuminate\Support\Facades\Http::preventStrayRequests();
app('view')->addNamespace('backups',$root.'/extension/Backups/resources/views');
$request=Illuminate\Http\Request::create('https://billing.example.test/admin/backups','GET');app()->instance('request',$request);Illuminate\Support\Facades\Facade::clearResolvedInstance('request');
$page=new Paymenter\Extensions\Others\Backups\Admin\Pages\Backups;$page->setId('diagnostics-fixture');
$command='* * * * * /usr/bin/flock -n /home/fixture/paymenter-backups-private/queue-worker.lock /usr/bin/php /home/fixture/paymenter/artisan queue:work --stop-when-empty --max-time=50';
$overview=['initialized'=>true,'configuration'=>['available'=>true,'valid'=>true,'checks'=>['jobs'=>['state'=>'ok','crontab_user'=>'fixture','items'=>[['kind'=>'queue-worker','command'=>$command,'coordination'=>'uncoordinated','source'=>'crontab']],'suggestions'=>['scheduler'=>$command.' --scheduler','queue'=>$command.' --queue']],'stores'=>[]]]];
(new ReflectionProperty($page,'overviewCache'))->setValue($page,$overview);
$rows=(new ReflectionMethod($page,'detectedChecks'))->invoke($page);
$section=Paymenter\Extensions\Others\Backups\Admin\Support\Sheet::section('Full backup diagnostics')->description('Detected commands and storage settings, for debugging full backups.')->schema($rows);
$body=Filament\Schemas\Schema::make($page)->components([$section])->toHtml();
$css=file_get_contents($base.'/vendor/filament/filament/dist/theme.css');
echo '<!doctype html><html class="dark"><head><meta charset="utf-8"><style>'.$css.'</style><style>body{background:#0b132b;color:#f7fbff}main{width:280px;margin:32px}h1{font-size:18px;margin-bottom:20px}</style></head><body><main class="domains-rail"><h1>Full backup diagnostics</h1>'.$body.'</main></body></html>';
'''
BROWSER = r'''
const fs=require('fs');const {chromium}=require(process.env.BACKUPS_TEST_PLAYWRIGHT_MODULE);
(async()=>{const browser=await chromium.launch({headless:true,args:['--no-sandbox','--disable-dev-shm-usage']});
try{const page=await browser.newPage({viewport:{width:390,height:1000},colorScheme:'dark',reducedMotion:'reduce'});await page.route('**/*',r=>r.abort());
await page.setContent(fs.readFileSync(process.argv[1],'utf8'),{waitUntil:'domcontentloaded'});
const checks=await page.locator('.fi-sc-text.fi-font-mono').evaluateAll(nodes=>nodes.map(e=>{const r=e.getBoundingClientRect();const c=getComputedStyle(e);return {text:e.textContent.trim(),width:r.width,right:r.right,scroll:e.scrollWidth,client:e.clientWidth,overflow:c.overflowX,whiteSpace:c.whiteSpace};}));
await page.screenshot({path:process.argv[1]+'.png',fullPage:true});fs.writeFileSync(process.argv[1]+'.json',JSON.stringify(checks,null,2));
if(checks.length<3)throw new Error('Native fixture did not render detected and suggested commands');
for(const c of checks){if(c.width>281||c.right>313||c.scroll>c.client+1)throw new Error('Diagnostics command needs horizontal scrolling in the narrow rail: '+JSON.stringify(c));}
console.log(JSON.stringify(checks));}finally{await browser.close();}})().catch(e=>{console.error(e.message);process.exitCode=1;});
'''


@unittest.skipUnless(all(os.environ.get(key) for key in ('PAYMENTER_TEST_APP', 'PHP_BINARY', 'BACKUPS_TEST_NODE', 'BACKUPS_TEST_PLAYWRIGHT_MODULE')),
                     'Pinned native Paymenter, PHP and offline Playwright required')
class DiagnosticsLayout(unittest.TestCase):
    def test_native_commands_stay_readable_in_a_narrow_rail(self):
        # A real Filament render is necessary: CSS on a hand-written span misses
        # its native schema grid and command wrappers, where this clipping occurs.
        source = Path(os.environ['PAYMENTER_TEST_APP'])
        self.assertEqual(subprocess.check_output(['git', '-C', str(source), 'rev-parse', 'HEAD'], text=True).strip(),
                         'ee729f7d99ac18942625b79f3f93d8d8b21255e8')
        self.assertEqual(hashlib.sha256((source / 'composer.lock').read_bytes()).hexdigest(),
                         'fd6bdeb5557b3af0976d5688d92081abc6f7c3235806a6c4b77f4b8bca9186be')
        output = Path(os.environ['BACKUPS_RENDER_DIR']) if os.environ.get('BACKUPS_RENDER_DIR') else Path(tempfile.mkdtemp(prefix='backups-diagnostics-'))
        output.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix='backups-diagnostics-native-') as scratch:
            work = Path(scratch)
            archive = work / 'source.tar'
            with archive.open('wb') as stream:
                subprocess.run(['git', '-C', str(source), 'archive', 'HEAD'], stdout=stream, check=True)
            app = work / 'app'
            app.mkdir()
            with tarfile.open(archive) as package:
                package.extractall(app, filter='data')
            (app / 'vendor').symlink_to(source / 'vendor', target_is_directory=True)
            for area in ('bootstrap/cache', 'storage/framework/cache', 'storage/framework/views', 'storage/framework/sessions', 'storage/logs'):
                (app / area).mkdir(parents=True, exist_ok=True)
            helper = work / 'render.php'
            helper.write_text(PHP)
            env = dict(os.environ, APP_ENV='testing', APP_KEY='base64:'+'dHR0'*10+'dHQ=', APP_URL='https://billing.example.test',
                       DB_CONNECTION='sqlite', DB_DATABASE=':memory:', CACHE_STORE='array', SESSION_DRIVER='array',
                       QUEUE_CONNECTION='sync', MAIL_MAILER='array', TELEMETRY_ENABLED='false')
            rendered = subprocess.run([os.environ['PHP_BINARY'], str(helper), str(app), str(ROOT)], env=env, capture_output=True, timeout=30)
            self.assertEqual(rendered.returncode, 0, rendered.stderr.decode()+rendered.stdout.decode())
            html = output / 'diagnostics.html'
            html.write_bytes(rendered.stdout)
            checked = subprocess.run([os.environ['BACKUPS_TEST_NODE'], '-e', BROWSER, str(html)], capture_output=True, timeout=25)
            self.assertEqual(checked.returncode, 0, checked.stderr.decode()+checked.stdout.decode())
