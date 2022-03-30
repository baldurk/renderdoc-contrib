#version 420 core

/////////////////////////////////////
//            Constants            //
/////////////////////////////////////

// possible values (these are only return values from this function, NOT texture binding points):
// RD_TextureType_1D
// RD_TextureType_2D
// RD_TextureType_3D
// RD_TextureType_Cube (OpenGL only)
// RD_TextureType_1D_Array (OpenGL only)
// RD_TextureType_2D_Array (OpenGL only)
// RD_TextureType_Cube_Array (OpenGL only)
// RD_TextureType_Rect (OpenGL only)
// RD_TextureType_Buffer (OpenGL only)
// RD_TextureType_2DMS
// RD_TextureType_2DMS_Array (OpenGL only)
uint RD_TextureType();

// selected sample, or -numSamples for resolve
int RD_SelectedSample();

uint RD_SelectedSliceFace();

uint RD_SelectedMip();

// xyz = width, height, depth (or array size). w = # mips
uvec4 RD_TexDim();

// x = horizontal downsample rate (1 full rate, 2 half rate)
// y = vertical downsample rate
// z = number of planes in input texture
// w = number of bits per component (8, 10, 16)
uvec4 RD_YUVDownsampleRate();

// x = where Y channel comes from
// y = where U channel comes from
// z = where V channel comes from
// w = where A channel comes from
// each index will be [0,1,2,3] for xyzw in first plane,
// [4,5,6,7] for xyzw in second plane texture, etc.
// it will be 0xff = 255 if the channel does not exist.
uvec4 RD_YUVAChannels();

// a pair with minimum and maximum selected range values
vec2 RD_SelectedRange();

/////////////////////////////////////


/////////////////////////////////////
//           Resources             //
/////////////////////////////////////

// Float Textures
layout (binding = RD_FLOAT_1D_ARRAY_BINDING) uniform sampler1DArray tex1DArray;
layout (binding = RD_FLOAT_2D_ARRAY_BINDING) uniform sampler2DArray tex2DArray;
layout (binding = RD_FLOAT_3D_BINDING) uniform sampler3D tex3D;
layout (binding = RD_FLOAT_2DMS_ARRAY_BINDING) uniform sampler2DMSArray tex2DMSArray;

// YUV textures only supported on vulkan
#ifdef VULKAN
layout(binding = RD_FLOAT_YUV_ARRAY_BINDING) uniform sampler2DArray texYUVArray[2];
#endif

// OpenGL has more texture types to match
#ifndef VULKAN
layout (binding = RD_FLOAT_1D_BINDING) uniform sampler1D tex1D;
layout (binding = RD_FLOAT_2D_BINDING) uniform sampler2D tex2D;
layout (binding = RD_FLOAT_CUBE_BINDING) uniform samplerCube texCube;
layout (binding = RD_FLOAT_CUBE_ARRAY_BINDING) uniform samplerCubeArray texCubeArray;
layout (binding = RD_FLOAT_RECT_BINDING) uniform sampler2DRect tex2DRect;
layout (binding = RD_FLOAT_BUFFER_BINDING) uniform samplerBuffer texBuffer;
layout (binding = RD_FLOAT_2DMS_BINDING) uniform sampler2DMS tex2DMS;
#endif

// Int Textures
layout (binding = RD_INT_1D_ARRAY_BINDING) uniform isampler1DArray texSInt1DArray;
layout (binding = RD_INT_2D_ARRAY_BINDING) uniform isampler2DArray texSInt2DArray;
layout (binding = RD_INT_3D_BINDING) uniform isampler3D texSInt3D;
layout (binding = RD_INT_2DMS_ARRAY_BINDING) uniform isampler2DMSArray texSInt2DMSArray;

#ifndef VULKAN
layout (binding = RD_INT_1D_BINDING) uniform isampler1D texSInt1D;
layout (binding = RD_INT_2D_BINDING) uniform isampler2D texSInt2D;
layout (binding = RD_INT_RECT_BINDING) uniform isampler2DRect texSInt2DRect;
layout (binding = RD_INT_BUFFER_BINDING) uniform isamplerBuffer texSIntBuffer;
layout (binding = RD_INT_2DMS_BINDING) uniform isampler2DMS texSInt2DMS;
#endif

// Unsigned int Textures
layout (binding = RD_UINT_1D_ARRAY_BINDING) uniform usampler1DArray texUInt1DArray;
layout (binding = RD_UINT_2D_ARRAY_BINDING) uniform usampler2DArray texUInt2DArray;
layout (binding = RD_UINT_3D_BINDING) uniform usampler3D texUInt3D;
layout (binding = RD_UINT_2DMS_ARRAY_BINDING) uniform usampler2DMSArray texUInt2DMSArray;

#ifndef VULKAN
layout (binding = RD_UINT_1D_BINDING) uniform usampler1D texUInt1D;
layout (binding = RD_UINT_2D_BINDING) uniform usampler2D texUInt2D;
layout (binding = RD_UINT_RECT_BINDING) uniform usampler2DRect texUInt2DRect;
layout (binding = RD_UINT_BUFFER_BINDING) uniform usamplerBuffer texUIntBuffer;
layout (binding = RD_UINT_2DMS_BINDING) uniform usampler2DMS texUInt2DMS;
#endif

/////////////////////////////////////

vec4 get_pixel_col(vec2 uv)
{
  vec4 col = vec4(0,0,0,0);

  int sampleCount = -RD_SelectedSample();

  uvec4 texRes = RD_TexDim();

  uint mip = RD_SelectedMip();
  uint sliceFace = RD_SelectedSliceFace();

  // RD_TexDim() is always the dimension of the texture. When loading from smaller mips, we need to multiply
  // uv by the mip dimension
  texRes.x = max(1u, texRes.x >> mip);
  texRes.y = max(1u, texRes.y >> mip);

// handle OpenGL-specific types, including non-arrayed versions
#ifndef VULKAN
  if(RD_TextureType() == RD_TextureType_1D)
  {
    return texelFetch(tex1D, int(uv.x * texRes.x), int(mip));
  }
  else if(RD_TextureType() == RD_TextureType_2D)
  {
    return texelFetch(tex2D, ivec2(uv * texRes.xy), int(mip));
  }
  else if(RD_TextureType() == RD_TextureType_Cube)
  {
    // don't handle cubemaps here, GL needs you to generate a cubemap lookup vector
  }
  else if(RD_TextureType() == RD_TextureType_Cube_Array)
  {
    // don't handle cubemaps here, GL needs you to generate a cubemap lookup vector
  }
  else if(RD_TextureType() == RD_TextureType_Rect)
  {
    return texelFetch(tex2DRect, ivec2(uv * texRes.xy));
  }
  else if(RD_TextureType() == RD_TextureType_Buffer)
  {
    return texelFetch(texBuffer, int(uv.x * texRes.x));
  }
  else if(RD_TextureType() == RD_TextureType_2DMS)
  {
    if(sampleCount < 0)
    {
      // worst resolve you've seen in your life
      for(int i = 0; i < sampleCount; i++)
        col += texelFetch(tex2DMS, ivec2(uv * texRes.xy), i);

      col /= float(sampleCount);

      return col;
    }
    else
    {
      return texelFetch(tex2DMS, ivec2(uv * texRes.xy), sampleCount);
    }
  }
#endif

  // we check for both array and non-array types here, since vulkan just
  // reports "1D" whereas GL will report "1D Array"
  if(RD_TextureType() == RD_TextureType_1D || RD_TextureType() == RD_TextureType_1D_Array)
  {
    return texelFetch(tex1DArray, ivec2(uv.x * texRes.x, sliceFace), int(mip));
  }
  else if(RD_TextureType() == RD_TextureType_2D || RD_TextureType() == RD_TextureType_2D_Array)
  {
    return texelFetch(tex2DArray, ivec3(uv * texRes.xy, sliceFace), int(mip));
  }
  else if(RD_TextureType() == RD_TextureType_3D)
  {
    col = texelFetch(tex3D, ivec3(uv * texRes.xy, sliceFace), int(mip));
  }
  else if(RD_TextureType() == RD_TextureType_2DMS || RD_TextureType() == RD_TextureType_2DMS_Array)
  {
    if(sampleCount < 0)
    {
      // worst resolve you've seen in your life
      for(int i = 0; i < sampleCount; i++)
        col += texelFetch(tex2DMSArray, ivec3(uv * texRes.xy, sliceFace), i);

      col /= float(sampleCount);
    }
    else
    {
      col = texelFetch(tex2DMSArray, ivec3(uv * texRes.xy, sliceFace), sampleCount);
    }
  }

  return col;
}

layout (location = 0) in vec2 uv;
layout (location = 0) out vec4 color_out;

void main()
{
  vec4 col = get_pixel_col(uv.xy);

  col.rgb = vec3(1.0f, 1.0f, 1.0f) - col.rgb;

  color_out = col;
}
