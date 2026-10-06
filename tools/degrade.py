# 카메라 촬영 시뮬레이션: 원근 왜곡 + 블러 + 색 틀어짐 + 노이즈 + JPEG
import sys, os, random, glob, io
import numpy as np
from PIL import Image, ImageFilter
def coeffs(src, dst):
    A=[];B=[]
    for (x,y),(u,v) in zip(dst,src):
        A.append([x,y,1,0,0,0,-u*x,-u*y]); B.append(u)
        A.append([0,0,0,x,y,1,-v*x,-v*y]); B.append(v)
    return np.linalg.solve(np.array(A,float),np.array(B,float)).tolist()
def degrade(img, W=1280, H=720, frac=0.6, tilt=0.06, blur=1.0, noise=5, q=75, rnd=random):
    img=img.convert('RGB')
    # 모니터 색 특성: 대비 감소 + 화이트밸런스
    a=np.asarray(img).astype(np.float32)
    lo=rnd.uniform(15,40); hi=rnd.uniform(200,240)
    wb=np.array([rnd.uniform(0.9,1.05),1.0,rnd.uniform(0.9,1.1)])
    a=(lo+(a/255.0)**rnd.uniform(0.85,1.15)*(hi-lo))*wb
    img=Image.fromarray(np.clip(a,0,255).astype(np.uint8))
    side=int(H*frac); cx=W/2+rnd.uniform(-0.1,0.1)*W; cy=H/2+rnd.uniform(-0.05,0.05)*H
    s=side/2
    dst=[(cx-s+rnd.uniform(-tilt,tilt)*side, cy-s+rnd.uniform(-tilt,tilt)*side),
         (cx+s+rnd.uniform(-tilt,tilt)*side, cy-s+rnd.uniform(-tilt,tilt)*side),
         (cx+s+rnd.uniform(-tilt,tilt)*side, cy+s+rnd.uniform(-tilt,tilt)*side),
         (cx-s+rnd.uniform(-tilt,tilt)*side, cy+s+rnd.uniform(-tilt,tilt)*side)]
    w,h=img.size
    c=coeffs([(0,0),(w,0),(w,h),(0,h)],dst)
    warped=img.transform((W,H),Image.PERSPECTIVE,c,Image.BILINEAR,fillcolor=(40,40,45))
    if blur>0: warped=warped.filter(ImageFilter.GaussianBlur(blur))
    a=np.asarray(warped).astype(np.float32)+np.random.normal(0,noise,(H,W,3))
    out=Image.fromarray(np.clip(a,0,255).astype(np.uint8))
    b=io.BytesIO(); out.save(b,'JPEG',quality=q); return b.getvalue()
if __name__=='__main__':
    src,dst=sys.argv[1],sys.argv[2]; kw=eval('dict(%s)'%(sys.argv[3] if len(sys.argv)>3 else ''))
    os.makedirs(dst,exist_ok=True); rnd=random.Random(7); np.random.seed(7)
    for f in sorted(glob.glob(src+'/*.png')):
        open(os.path.join(dst,os.path.basename(f)[:-4]+'.jpg'),'wb').write(degrade(Image.open(f),rnd=rnd,**kw))
