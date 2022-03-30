

/////////////////////////////////////
//            Constants            //
/////////////////////////////////////

// possible values (these are only return values from this function, NOT texture binding points):
// RD_TextureType_1D
// RD_TextureType_2D
// RD_TextureType_3D
// RD_TextureType_Depth
// RD_TextureType_DepthStencil
// RD_TextureType_DepthMS
// RD_TextureType_DepthStencilMS
uint RD_TextureType();

// selected sample, or -numSamples for resolve
int RD_SelectedSample();

uint RD_SelectedSliceFace();

uint RD_SelectedMip();

// xyz = width, height, depth. w = # mips
uint4 RD_TexDim();

// x = horizontal downsample rate (1 full rate, 2 half rate)
// y = vertical downsample rate
// z = number of planes in input texture
// w = number of bits per component (8, 10, 16)
uint4 RD_YUVDownsampleRate();

// x = where Y channel comes from
// y = where U channel comes from
// z = where V channel comes from
// w = where A channel comes from
// each index will be [0,1,2,3] for xyzw in first plane,
// [4,5,6,7] for xyzw in second plane texture, etc.
// it will be 0xff = 255 if the channel does not exist.
uint4 RD_YUVAChannels();

// a pair with minimum and maximum selected range values
float2 RD_SelectedRange();

/////////////////////////////////////

/////////////////////////////////////
//           Resources             //
/////////////////////////////////////

// Float Textures
Texture1DArray<float4> texDisplayTex1DArray : register(RD_FLOAT_1D_ARRAY_BINDING);
Texture2DArray<float4> texDisplayTex2DArray : register(RD_FLOAT_2D_ARRAY_BINDING);
Texture3D<float4> texDisplayTex3D : register(RD_FLOAT_3D_BINDING);
Texture2DMSArray<float4> texDisplayTex2DMSArray : register(RD_FLOAT_2DMS_ARRAY_BINDING);
Texture2DArray<float4> texDisplayYUVArray : register(RD_FLOAT_YUV_ARRAY_BINDING);

// only used on D3D
Texture2DArray<float2> texDisplayTexDepthArray : register(RD_FLOAT_DEPTH_ARRAY_BINDING);
Texture2DArray<uint2> texDisplayTexStencilArray : register(RD_FLOAT_STENCIL_ARRAY_BINDING);
Texture2DMSArray<float2> texDisplayTexDepthMSArray : register(RD_FLOAT_DEPTHMS_ARRAY_BINDING);
Texture2DMSArray<uint2> texDisplayTexStencilMSArray : register(RD_FLOAT_STENCILMS_ARRAY_BINDING);

/*
// Int Textures
Texture1DArray<int4> texDisplayIntTex1DArray : register(RD_INT_1D_ARRAY_BINDING);
Texture2DArray<int4> texDisplayIntTex2DArray : register(RD_INT_2D_ARRAY_BINDING);
Texture3D<int4> texDisplayIntTex3D : register(RD_INT_3D_BINDING);
Texture2DMSArray<int4> texDisplayIntTex2DMSArray : register(RD_INT_2DMS_ARRAY_BINDING);

// Unsigned int Textures
Texture1DArray<uint4> texDisplayUIntTex1DArray : register(RD_UINT_1D_ARRAY_BINDING);
Texture2DArray<uint4> texDisplayUIntTex2DArray : register(RD_UINT_2D_ARRAY_BINDING);
Texture3D<uint4> texDisplayUIntTex3D : register(RD_UINT_3D_BINDING);
Texture2DMSArray<uint4> texDisplayUIntTex2DMSArray : register(RD_UINT_2DMS_ARRAY_BINDING);
*/

/////////////////////////////////////

/////////////////////////////////////
//            Samplers             //
/////////////////////////////////////

SamplerState pointSampler : register(RD_POINT_SAMPLER_BINDING);
SamplerState linearSampler : register(RD_LINEAR_SAMPLER_BINDING);

/////////////////////////////////////


float4 get_pixel_col(float2 uv)
{
  float4 col = float4(0,0,0,0);

  // local copy so we can invert/clamp it
  int sample = RD_SelectedSample();

  uint4 texRes = RD_TexDim();

  uint mip = RD_SelectedMip();
  uint sliceFace = RD_SelectedSliceFace();

  // RD_TexDim() is always the dimension of the texture. When loading from smaller mips, we need to multiply
  // uv by the mip dimension
  texRes.x = max(1, texRes.x >> mip);
  texRes.y = max(1, texRes.y >> mip);

  if(RD_TextureType() == RD_TextureType_1D)
  {
    col = texDisplayTex1DArray.Load(int3(uv.x * texRes.x, sliceFace, mip));
  }
  else if(RD_TextureType() == RD_TextureType_2D)
  {
    col = texDisplayTex2DArray.Load(int4(uv.xy * texRes.xy, sliceFace, mip));
  }
  else if(RD_TextureType() == RD_TextureType_3D)
  {
    col = texDisplayTex3D.Load(int4(uv.xy * texRes.xy, sliceFace, mip));
  }
  else if(RD_TextureType() == RD_TextureType_Depth)
  {
    col.r = texDisplayTexDepthArray.Load(int4(uv.xy * texRes.xy, sliceFace, mip)).r;
    col.gba = float3(0, 0, 1);
  }
  else if(RD_TextureType() == RD_TextureType_DepthStencil)
  {
    col.r = texDisplayTexDepthArray.Load(int4(uv.xy * texRes.xy, sliceFace, mip)).r;
    col.g = texDisplayTexStencilArray.Load(int4(uv.xy * texRes.xy, sliceFace, mip)).g / 255.0f;
    col.ba = float2(0, 1);
  }
  else if(RD_TextureType() == RD_TextureType_DepthMS)
  {
    // sample = -1 means 'average', we'll just return sample 0
    if(sample < 0)
      sample = 0;

    col.r = texDisplayTexDepthMSArray.Load(int3(uv.xy * texRes.xy, sliceFace), sample).r;
    col.gba = float3(0, 0, 1);
  }
  else if(RD_TextureType() == RD_TextureType_DepthStencilMS)
  {
    // sample = -1 means 'average', we'll just return sample 0
    if(sample < 0)
      sample = 0;

    col.r = texDisplayTexDepthMSArray.Load(int3(uv.xy * texRes.xy, sliceFace), sample).r;
    col.g = texDisplayTexStencilMSArray.Load(int3(uv.xy * texRes.xy, sliceFace), sample).g / 255.0f;
    col.ba = float2(0, 1);
  }
  else if(RD_TextureType() == RD_TextureType_2DMS)
  {
    // sample = -1 means 'average'
    if(sample < 0)
    {
      int sampleCount = -sample;

      // worst resolve you've seen in your life
      for(int i = 0; i < sampleCount; i++)
        col += texDisplayTex2DMSArray.Load(int3(uv.xy * texRes.xy, sliceFace), i);

      col /= float(sampleCount);
    }
    else
    {
      col = texDisplayTex2DMSArray.Load(int3(uv.xy * texRes.xy, sliceFace), sample);
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
