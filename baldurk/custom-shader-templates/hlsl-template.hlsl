// This is an HLSL display shader for D3D11 and D3D12

// 1 = 1D, 2 = 2D, 3 = 3D, 4 = Depth, 5 = Depth + Stencil
// 6 = Depth (MS), 7 = Depth + Stencil (MS)
uint RENDERDOC_TextureType;

// selected MSAA sample or -numSamples for resolve. See docs
int RENDERDOC_SelectedSample;

// selected array slice or cubemap face in UI
uint RENDERDOC_SelectedSliceFace;

// selected mip in UI
uint RENDERDOC_SelectedMip;

// xyz == width, height, depth. w == # mips
uint4 RENDERDOC_TexDim;
uint4 RENDERDOC_YUVDownsampleRate;
uint4 RENDERDOC_YUVAChannels;

// Textures
Texture1DArray<float4> texDisplayTex1DArray : register(t1);
Texture2DArray<float4> texDisplayTex2DArray : register(t2);
Texture3D<float4> texDisplayTex3D : register(t3);
Texture2DArray<float2> texDisplayTexDepthArray : register(t4);
Texture2DArray<uint2> texDisplayTexStencilArray : register(t5);
Texture2DMSArray<float2> texDisplayTexDepthMSArray : register(t6);
Texture2DMSArray<uint2> texDisplayTexStencilMSArray : register(t7);
Texture2DMSArray<float4> texDisplayTex2DMSArray : register(t9);
Texture2DArray<float4> texDisplayYUVArray : register(t10);

/*
// Unsigned int textures
Texture1DArray<uint4> texDisplayUIntTex1DArray : register(t11);
Texture2DArray<uint4> texDisplayUIntTex2DArray : register(t12);
Texture3D<uint4> texDisplayUIntTex3D : register(t13);
Texture2DMSArray<uint4> texDisplayUIntTex2DMSArray : register(t19);

// Int textures
Texture1DArray<int4> texDisplayIntTex1DArray : register(t21);
Texture2DArray<int4> texDisplayIntTex2DArray : register(t22);
Texture3D<int4> texDisplayIntTex3D : register(t23);
Texture2DMSArray<int4> texDisplayIntTex2DMSArray : register(t29);
*/

// End Textures

// Samplers
SamplerState pointSampler : register(s0);
SamplerState linearSampler : register(s1);
// End Samplers

float4 get_pixel_col(float2 uv)
{
  float4 col = float4(0,0,0,0);

  // local copy so we can invert/clamp it
  int sample = RENDERDOC_SelectedSample;

  uint4 texRes = RENDERDOC_TexDim;

  // RENDERDOC_TexDim is always the dimension of the texture. When loading from smaller mips, we need to multiply
  // uv by the mip dimension
  texRes.x = max(1, texRes.x >> RENDERDOC_SelectedMip);
  texRes.y = max(1, texRes.y >> RENDERDOC_SelectedMip);

  if(RENDERDOC_TextureType == 1)
  {
    col = texDisplayTex1DArray.Load(int3(uv.x * texRes.x, RENDERDOC_SelectedSliceFace, RENDERDOC_SelectedMip));
  }
  else if(RENDERDOC_TextureType == 2)
  {
    col = texDisplayTex2DArray.Load(int4(uv.xy * texRes.xy, RENDERDOC_SelectedSliceFace, RENDERDOC_SelectedMip));
  }
  else if(RENDERDOC_TextureType == 3)
  {
    col = texDisplayTex3D.Load(int4(uv.xy * texRes.xy, RENDERDOC_SelectedSliceFace, RENDERDOC_SelectedMip));
  }
  else if(RENDERDOC_TextureType == 4)
  {
    col.r = texDisplayTexDepthArray.Load(int4(uv.xy * texRes.xy, RENDERDOC_SelectedSliceFace, RENDERDOC_SelectedMip)).r;
    col.gba = float3(0, 0, 1);
  }
  else if(RENDERDOC_TextureType == 5)
  {
    col.r = texDisplayTexDepthArray.Load(int4(uv.xy * texRes.xy, RENDERDOC_SelectedSliceFace, RENDERDOC_SelectedMip)).r;
    col.g = texDisplayTexStencilArray.Load(int4(uv.xy * texRes.xy, RENDERDOC_SelectedSliceFace, RENDERDOC_SelectedMip)).g / 255.0f;
    col.ba = float2(0, 1);
  }
  else if(RENDERDOC_TextureType == 6)
  {
    // sample = -1 means 'average', we'll just return sample 0
    if(sample < 0)
      sample = 0;

    col.r = texDisplayTexDepthMSArray.Load(int3(uv.xy * texRes.xy, RENDERDOC_SelectedSliceFace), sample).r;
    col.gba = float3(0, 0, 1);
  }
  else if(RENDERDOC_TextureType == 7)
  {
    // sample = -1 means 'average', we'll just return sample 0
    if(sample < 0)
      sample = 0;

    col.r = texDisplayTexDepthMSArray.Load(int3(uv.xy * texRes.xy, RENDERDOC_SelectedSliceFace), sample).r;
    col.g = texDisplayTexStencilMSArray.Load(int3(uv.xy * texRes.xy, RENDERDOC_SelectedSliceFace), sample).g / 255.0f;
    col.ba = float2(0, 1);
  }
  else if(RENDERDOC_TextureType == 9)
  {
    // sample = -1 means 'average'
    if(sample < 0)
    {
      int sampleCount = -sample;

      // worst resolve you've seen in your life
      for(int i = 0; i < sampleCount; i++)
        col += texDisplayTex2DMSArray.Load(int3(uv.xy * texRes.xy, RENDERDOC_SelectedSliceFace), i);

      col /= float(sampleCount);
    }
    else
    {
      col = texDisplayTex2DMSArray.Load(int3(uv.xy * texRes.xy, RENDERDOC_SelectedSliceFace), sample);
    }
  }
  return col;
}

float4 main(float4 pos : SV_Position, float4 UV : TEXCOORD0) : SV_Target0
{
  float4 col = get_pixel_col(UV.xy);

  col.rgb = 1.0f.xxx - col.rgb;

  return col;
}
