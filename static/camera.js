(function(global){
  'use strict';
  let stream=null;
  // 台灯摄像头物理安装方向固定，默认旋转 180°；用户无需理解或设置方向。
  // 浏览器画面已经按设备方向返回，默认不再额外旋转，避免画面倒置。
  let rotated=true,mirrored=false,attachedVideo=null;

  function apply(video){
    if(video)attachedVideo=video;
    if(attachedVideo){
      attachedVideo.style.transform=(rotated?'rotate(180deg) ':'')+(mirrored?'scaleX(-1)':'');
    }
  }

  function rotate180(){rotated=!rotated;apply();return state();}
  function mirror(){mirrored=!mirrored;apply();return state();}
  function reset(){rotated=false;mirrored=false;apply();return state();}
  function state(){return {rotated,mirrored};}

  function isLocalhost(){
    return /^(localhost|127(?:\.\d+){3}|\[::1\])$/.test(location.hostname);
  }

  function support(){
    const secure=global.isSecureContext||isLocalhost();
    if(!secure)return {supported:false,secure:false,reason:'摄像头需要 HTTPS 或 localhost 安全环境。'};
    if(!navigator.mediaDevices||!navigator.mediaDevices.getUserMedia){
      return {supported:false,secure:true,reason:'当前浏览器不支持网页摄像头，请使用“从相册选择”。'};
    }
    return {supported:true,secure:true,reason:''};
  }

  function errorMessage(error){
    const name=error&&error.name;
    if(name==='NotAllowedError'||name==='SecurityError')return '摄像头权限被拒绝，请在浏览器设置中允许后重试。';
    if(name==='NotFoundError')return '没有找到可用摄像头，请使用“从相册选择”。';
    if(name==='NotReadableError'||name==='AbortError')return '摄像头正被其他应用占用，请关闭占用程序后重试。';
    if(name==='OverconstrainedError')return '摄像头不支持当前分辨率，请重试或从相册选择。';
    return '摄像头启动失败，请重试或从相册选择。';
  }

  function stop(){
    if(stream)stream.getTracks().forEach(track=>track.stop());
    stream=null;
    attachedVideo=null;
  }

  async function open(video){
    const status=support();
    if(!status.supported)throw new Error(status.reason);
    stop();
    try{
      stream=await navigator.mediaDevices.getUserMedia({
        audio:false,
        video:{facingMode:{ideal:'environment'},width:{ideal:1280},height:{ideal:720}}
      });
      video.muted=true;
      video.playsInline=true;
      video.srcObject=stream;
      await video.play();
      apply(video);
      return stream;
    }catch(error){
      stop();
      const wrapped=new Error(errorMessage(error));
      wrapped.cause=error;
      throw wrapped;
    }
  }

  function capture(video,options){
    options=options||{};
    const sourceWidth=video.videoWidth||0,sourceHeight=video.videoHeight||0;
    if(!sourceWidth||!sourceHeight)return Promise.reject(new Error('摄像头画面尚未准备好。'));
    const maxSide=Math.max(640,Math.min(2400,options.maxSide||1600));
    const scale=Math.min(1,maxSide/Math.max(sourceWidth,sourceHeight));
    const canvas=document.createElement('canvas');
    canvas.width=Math.max(1,Math.round(sourceWidth*scale));
    canvas.height=Math.max(1,Math.round(sourceHeight*scale));
    const context=canvas.getContext('2d',{alpha:false});
    context.save();
    context.translate(canvas.width/2,canvas.height/2);
    if(rotated)context.rotate(Math.PI);
    if(mirrored)context.scale(-1,1);
    context.drawImage(video,-canvas.width/2,-canvas.height/2,canvas.width,canvas.height);
    context.restore();
    const quality=Math.max(.6,Math.min(.95,options.quality||.88));
    return new Promise((resolve,reject)=>canvas.toBlob(
      blob=>blob?resolve(blob):reject(new Error('拍照失败，请重试。')),
      'image/jpeg',quality
    ));
  }

  global.addEventListener('pagehide',stop);
  global.DictationCamera={support,open,capture,stop,errorMessage,rotate180,mirror,reset,state,apply};
})(window);
