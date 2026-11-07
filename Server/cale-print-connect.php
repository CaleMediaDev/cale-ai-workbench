<?php
/** Plugin Name: Cale AI Print Farm eBay Connect */
namespace CalePrintConnect;
defined('ABSPATH') || exit;
const SCOPE = 'https://api.ebay.com/oauth/api_scope/sell.fulfillment.readonly';
function root(){return dirname(rtrim(ABSPATH,'/')).'/.cale-print-connect';}
function config(){ $p=root().'/config.json';return is_file($p)?json_decode(file_get_contents($p),true):[]; }
function result($data,$status=200){$r=new \WP_REST_Response($data,$status);$r->header('Cache-Control','no-store, private');$r->header('Referrer-Policy','no-referrer');return $r;}
function failure($text,$status=400){return result(['message'=>$text],$status);}
function valid_id($id){return is_string($id)&&preg_match('/^[a-f0-9]{48}$/D',$id);}
function save($id,$data){$p=root().'/sessions/'.$id.'.json';$temp=$p.'.'.bin2hex(random_bytes(8));if(file_put_contents($temp,wp_json_encode($data),LOCK_EX)===false)throw new \RuntimeException('Storage unavailable');chmod($temp,0600);rename($temp,$p);}
function locked($id,$fn){if(!valid_id($id))return failure('Invalid connection');$p=root().'/sessions/'.$id.'.json';if(!is_file($p))return failure('Connection expired',404);$lock=fopen(root().'/sessions/'.$id.'.lock','c');if(!$lock||!flock($lock,LOCK_EX))return failure('Connection busy',503);try{$data=json_decode(file_get_contents($p),true);return $fn($data);}finally{flock($lock,LOCK_UN);fclose($lock);}}
function authorized($data,$verifier){return is_string($verifier)&&strlen($verifier)>=40&&strlen($verifier)<=128&&hash_equals($data['challenge']??'',hash('sha256',$verifier));}
function crypt_token($token,$decode=false){$key=hex2bin(config()['encryption_key']??'');if(strlen($key)!==32)throw new \RuntimeException('Encryption unavailable');if($decode){$raw=base64_decode($token,true);if(!$raw||strlen($raw)<29)throw new \RuntimeException('Invalid token');$plain=openssl_decrypt(substr($raw,28),'aes-256-gcm',$key,OPENSSL_RAW_DATA,substr($raw,0,12),substr($raw,12,16));if($plain===false)throw new \RuntimeException('Cannot decrypt token');return $plain;}$iv=random_bytes(12);$tag='';$cipher=openssl_encrypt($token,'aes-256-gcm',$key,OPENSSL_RAW_DATA,$iv,$tag);if($cipher===false)throw new \RuntimeException('Cannot protect token');return base64_encode($iv.$tag.$cipher);}
function exchange($fields){$c=config();$reply=wp_remote_post('https://api.ebay.com/identity/v1/oauth2/token',['timeout'=>25,'redirection'=>0,'headers'=>['Authorization'=>'Basic '.base64_encode($c['client_id'].':'.$c['client_secret']),'Content-Type'=>'application/x-www-form-urlencoded'],'body'=>http_build_query($fields)]);if(is_wp_error($reply)||wp_remote_retrieve_response_code($reply)!==200)throw new \RuntimeException('eBay authorization failed');$data=json_decode(wp_remote_retrieve_body($reply),true);if(empty($data['access_token'])||empty($data['expires_in']))throw new \RuntimeException('No eBay token');return $data;}
add_action('rest_api_init',function(){
 $ns='cale-print-connect/v1';
 register_rest_route($ns,'/begin',['methods'=>'POST','permission_callback'=>'__return_true','callback'=>function($r){
  $c=config();if(empty($c['client_id'])||empty($c['client_secret'])||empty($c['runame']))return failure('eBay connection setup is not complete yet.',503);
  $challenge=$r['challenge'];if(!is_string($challenge)||!preg_match('/^[a-f0-9]{64}$/D',$challenge))return failure('Invalid device challenge');
  $ip=hash('sha256',$_SERVER['REMOTE_ADDR']??'unknown');$rate='cale_pf_connect_'.substr($ip,0,24);$count=(int)get_transient($rate);if($count>=10)return failure('Please wait a minute before reconnecting',429);set_transient($rate,$count+1,60);
  $id=bin2hex(random_bytes(24));save($id,['challenge'=>$challenge,'status'=>'pending','created'=>time(),'expires'=>time()+600]);
  return result(['id'=>$id,'url'=>'https://auth.ebay.com/oauth2/authorize?'.http_build_query(['client_id'=>$c['client_id'],'redirect_uri'=>$c['runame'],'response_type'=>'code','scope'=>SCOPE,'state'=>$id])]);
 }]);
 register_rest_route($ns,'/callback',['methods'=>'GET','permission_callback'=>'__return_true','callback'=>function($r){
  return locked($r['state'],function($data)use($r){
   if($data['status']!=='pending'||$data['expires']<time())return failure('This sign-in link has expired. Return to Cale AI and reconnect.');
   if($r['error']){$data['status']='denied';save($r['state'],$data);return result(['message'=>'Sign-in cancelled. You may close this tab.']);}
   $code=$r['code'];if(!is_string($code)||strlen($code)<10||strlen($code)>4096)return failure('Missing eBay authorization code');
   // Consume the pending state before exchanging the one-use code.
   $data['status']='exchanging';save($r['state'],$data);
   try{$tokens=exchange(['grant_type'=>'authorization_code','code'=>$code,'redirect_uri'=>config()['runame']]);if(empty($tokens['refresh_token']))throw new \RuntimeException('Missing refresh token');
    $data['refresh']=crypt_token($tokens['refresh_token']);$data['status']='connected';$data['expires']=time()+(int)($tokens['refresh_token_expires_in']??86400);$data['connected']=time();save($r['state'],$data);
    wp_safe_redirect(home_url('/print-farm-connected/'));exit;
   }catch(\Throwable $e){$data['status']='failed';save($r['state'],$data);return failure('eBay sign-in could not finish. Return to Cale AI and reconnect.',502);}
  });
 }]);
 foreach(['poll','token','disconnect'] as $action)register_rest_route($ns,'/'.$action,['methods'=>'POST','permission_callback'=>'__return_true','callback'=>function($r)use($action){
  return locked($r['id'],function($data)use($r,$action){
   if(!authorized($data,$r['verifier']))return failure('Connection authorization failed',403);
   if($action==='disconnect'){$data=['challenge'=>$data['challenge'],'status'=>'disconnected','expires'=>time()];save($r['id'],$data);return result(['disconnected'=>true]);}
   if($data['expires']<time())return result(['status'=>'expired']);
   if($action==='poll')return result(['status'=>$data['status']]);
   if($data['status']!=='connected')return failure('Connect eBay first',401);
   if(time()-(int)($data['last_token']??0)<15)return failure('Please wait before refreshing again',429);
   try{$tokens=exchange(['grant_type'=>'refresh_token','refresh_token'=>crypt_token($data['refresh'],true),'scope'=>SCOPE]);$data['last_token']=time();save($r['id'],$data);return result(['access_token'=>$tokens['access_token'],'expires_in'=>(int)$tokens['expires_in']]);}catch(\Throwable $e){return failure('Reconnect your eBay account',401);}
  });
 }]);
});
add_action('template_redirect',function(){
 if(rtrim((string)parse_url($_SERVER['REQUEST_URI']??'',PHP_URL_PATH),'/')!=='/print-farm-connected')return;
 nocache_headers();status_header(200);header('Referrer-Policy: no-referrer');header("Content-Security-Policy: default-src 'none'; style-src 'unsafe-inline'; frame-ancestors 'none'");
 ?><!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>eBay connected · Cale AI</title><style>body{margin:0;background:#0b1119;color:#eef6fa;font:18px system-ui;min-height:100vh;display:grid;place-items:center}main{max-width:540px;padding:48px;margin:24px;background:#151e2b;border:1px solid #2b3b4e;border-radius:20px}small{color:#52dac1;letter-spacing:.15em;font-weight:700}h1{font-size:38px;letter-spacing:-.04em}p{color:#b8c6d5;line-height:1.7}.check{color:#52dac1;font-size:42px}</style><main><small>CALE AI / PRINT FARM</small><div class="check">✓</div><h1>eBay is connected.</h1><p>Return to Cale AI to see your sales, connect your printers and manage your print queue.</p><p>You can close this tab.</p></main></html><?php exit;
},-200);
